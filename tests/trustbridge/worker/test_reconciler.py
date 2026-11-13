"""The delivery reconciler and retention (0028 invariants 7 and 8)."""

import asyncio
from collections.abc import Sequence
from datetime import timedelta
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.trustbridge.fakes import FakeHookClient
from tests.trustbridge.helpers import host
from tests.trustbridge.worker.harness import Harness, key
from trustbridge.deletion.ports import PurgeResult
from trustbridge.worker import metrics
from trustbridge.worker import reconciler as reconciler_module
from trustbridge.worker.deliveries import DeliveryKey
from trustbridge.worker.health import Heartbeat
from trustbridge.worker.models import NIL_TENANT
from trustbridge.worker.reconciler import (
    DeliveryReconciler,
    HookDelivery,
    Reconciler,
    ReconcilerDeps,
)


def _hd(hook_id: int, guid: str, status: int, h: Harness) -> HookDelivery:
    return HookDelivery(id=hook_id, guid=guid, status_code=status, delivered_at=h.clock())


def _reconciler(h: Harness, client: FakeHookClient, **kw: int) -> DeliveryReconciler:
    return DeliveryReconciler(
        h.store,
        client,
        lookback=timedelta(hours=72),
        max_per_run=kw.get("max_per_run", 100),
        clock=h.clock,
    )


async def test_a_delivery_with_a_stored_row_is_never_redelivered() -> None:  # inv. 8
    """A delivery with a stored row is never redelivered."""
    h = await Harness({}).start()
    await h.add("g-stored")
    client = FakeHookClient(
        [_hd(1, "g-stored", 502, h), _hd(2, "g-missing", 502, h), _hd(3, "g-ok", 200, h)]
    )
    count = await _reconciler(h, client).reconcile_host(h.settings.github_hosts[0])
    assert count == 1
    assert client.redelivered == [2]  # only the failed delivery that has no row


async def test_a_stored_row_in_any_state_blocks_redelivery() -> None:  # inv. 8
    """A stored row in any state blocks redelivery."""
    h = await Harness({}).start()
    for guid in ("g-pending", "g-processed", "g-dead"):
        await h.add(guid)
    await h.store.mark_processed(key("g-processed"), h.clock(), attempts=0)
    await h.store.mark_dead(key("g-dead"), "x", h.clock(), attempts=0)
    client = FakeHookClient(
        [_hd(i, g, 500, h) for i, g in enumerate(("g-pending", "g-processed", "g-dead"))]
    )
    assert await _reconciler(h, client).reconcile_host(h.settings.github_hosts[0]) == 0
    assert client.redelivered == []


async def test_the_latest_attempt_decides_and_a_failed_redelivery_is_retried() -> None:
    """The latest attempt decides and a failed redelivery is retried."""
    h = await Harness({}).start()
    client = FakeHookClient(
        [
            _hd(10, "g-recovered", 200, h),  # newest first: the latest attempt succeeded
            _hd(9, "g-recovered", 502, h),
            _hd(8, "g-again", 504, h),  # the redelivery attempt failed too
            _hd(7, "g-again", 502, h),
        ]
    )
    assert await _reconciler(h, client).reconcile_host(h.settings.github_hosts[0]) == 1
    assert client.redelivered == [8]


async def test_a_row_stored_between_listing_and_redelivery_is_not_redelivered() -> None:  # inv. 8
    """A row stored between listing and redelivery is not redelivered."""
    h = await Harness({}).start()
    client = FakeHookClient([_hd(1, "g-race", 502, h)])
    real = h.store.existing_keys
    calls = {"n": 0}

    async def existing(keys: Sequence[DeliveryKey]) -> set[DeliveryKey]:
        calls["n"] += 1
        if calls["n"] == 1:
            return set()  # the batch check ran before the ingress stored it
        await h.add("g-race", relay=False)  # ... which then happens
        return await real(keys)

    h.store.existing_keys = existing  # type: ignore[method-assign]
    assert await _reconciler(h, client).reconcile_host(h.settings.github_hosts[0]) == 0
    assert client.redelivered == []


async def test_redeliveries_per_run_are_capped() -> None:
    """Redeliveries per run are capped."""
    h = await Harness({}).start()
    client = FakeHookClient([_hd(i, f"g{i}", 500, h) for i in range(5)])
    assert (
        await _reconciler(h, client, max_per_run=2).reconcile_host(h.settings.github_hosts[0]) == 2
    )


def _recon(h: Harness, client: FakeHookClient | None) -> Reconciler:
    return Reconciler(
        h.settings,
        ReconcilerDeps(
            store=h.store,
            queue=h.queue,
            relay=h.relay,
            hosts=h.hosts,
            leases=h.leases,
            deliveries=client,
            clock=h.clock,
        ),
    )


async def test_only_ready_hosts_are_reconciled_and_without_a_client_nothing_happens() -> None:
    """Only ready hosts are reconciled and without a client nothing happens."""
    h = await Harness({}, failing_hosts={"bad"}).start()
    client = FakeHookClient([_hd(1, "g1", 500, h)])
    assert await _recon(h, client).reconcile_deliveries() == 1  # "ok" only; "bad" is unavailable
    assert len(client.redelivered) == 1
    assert await _recon(h, None).reconcile_deliveries() == 0


async def test_a_second_replica_skips_a_host_whose_lease_is_held() -> None:  # EB-6 singleton duty
    """A second replica skips a host whose lease is held."""
    h = await Harness({}, hosts=[host("ok")]).start()
    client = FakeHookClient([_hd(1, "g1", 500, h)])
    async with h.leases.hold(NIL_TENANT, "_core", "reconcile-deliveries:ok"):
        assert await _recon(h, client).reconcile_deliveries() == 0
    assert client.redelivered == []
    assert await _recon(h, client).reconcile_deliveries() == 1  # lease free again


async def test_retention_purges_bodies_then_expired_ids_and_keeps_dead_bodies_longer() -> None:
    """Retention purges bodies then expired ids and keeps dead bodies longer."""
    h = await Harness({}).start()  # 7 d processed, 30 d dead, 30 d ids (the defaults)
    for delivery_id in ("processed", "dead", "pending"):
        await h.add(delivery_id)
    await h.store.mark_processed(key("processed"), h.clock(), attempts=0)
    await h.store.mark_dead(key("dead"), "x", h.clock(), attempts=0)
    recon = _recon(h, None)
    h.advance(8 * 86400)
    await recon.retention()
    processed, dead, pending = [await h.store.get(key(i)) for i in ("processed", "dead", "pending")]
    assert processed is not None
    assert processed.body is None  # purged, but the row remains to dedupe a late redelivery
    assert dead is not None
    assert dead.body is not None  # a dead body stays readable for its longer retention (inv. 7)
    assert pending is not None
    assert pending.body is not None  # a pending row is never touched
    h.advance(23 * 86400)  # 31 days after dead-lettering
    await recon.retention()
    assert await h.store.get(key("processed")) is None
    assert await h.store.get(key("dead")) is None
    assert (await h.store.get(key("pending"))) is not None


async def test_retention_is_a_singleton_duty() -> None:
    """Retention is a singleton duty."""
    h = await Harness({}).start()
    await h.add("d1")
    await h.store.mark_processed(key("d1"), h.clock(), attempts=0)
    h.advance(8 * 86400)
    async with h.leases.hold(NIL_TENANT, "_core", "retention"):
        await _recon(h, None).retention()
    assert (await h.store.get(key("d1"))).body is not None  # type: ignore[union-attr]


async def test_run_executes_each_job_and_stops_on_the_event() -> None:
    """Run executes each job and stops on the event."""
    h = await Harness({}).start()
    await h.add("d1", relay=False)
    stop = asyncio.Event()
    task = asyncio.create_task(_recon(h, None).run(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert set(h.valkey.ids_of("tb:events")) == {"d1"}  # the relay tick ran on start


SESSIONS = cast(async_sessionmaker[AsyncSession], object())


def _purger(
    h: Harness, sessions: async_sessionmaker[AsyncSession] | None = SESSIONS
) -> tuple[Reconciler, Heartbeat]:
    beat = Heartbeat()
    beat.register("audit-purge", 60)
    recon = Reconciler(
        h.settings,
        ReconcilerDeps(
            store=h.store,
            queue=h.queue,
            relay=h.relay,
            hosts=h.hosts,
            leases=h.leases,
            deliveries=None,
            clock=h.clock,
            heartbeat=beat,
            audit_sessions=sessions,
        ),
    )
    return recon, beat


def _patch_purge(monkeypatch: pytest.MonkeyPatch, batches: int, per_batch: int) -> list[Any]:
    calls: list[Any] = []

    async def fake(factory: object, **kw: Any) -> int:  # noqa: ANN401
        calls.append(kw)
        for _ in range(batches):
            kw["on_batch"](per_batch)
        return batches * per_batch

    monkeypatch.setattr(reconciler_module, "purge_expired", fake)
    return calls


async def test_audit_purge_passes_the_settings_and_beats_per_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The job hands the retention bounds to `purge_expired` and beats once per batch."""
    h = await Harness(
        {}, audit_retention_days=200, audit_purge_batch_size=7, audit_purge_max_batches=4
    ).start()
    calls = _patch_purge(monkeypatch, batches=3, per_batch=7)
    recon, beat = _purger(h)
    seen: list[str] = []
    monkeypatch.setattr(beat, "beat", seen.append)
    assert await recon.audit_purge() == 21
    (kw,) = calls
    assert (kw["retention_days"], kw["min_days"], kw["batch_size"], kw["max_batches"]) == (
        200,
        h.settings.audit_retention_min_days,
        7,
        4,
    )
    assert kw["now"] == h.clock()
    assert seen == ["audit-purge"] * 4  # one at the start, one per batch
    assert kw["budget_seconds"] == h.settings.lease_ttl_seconds / 2


async def test_audit_purge_accounts_each_batch_even_when_the_run_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Rows deleted before a mid-run failure are already counted and logged."""
    h = await Harness({}, audit_retention_days=200).start()
    added: list[int] = []
    monkeypatch.setattr(metrics.audit_purged, "add", added.append)

    async def failing(factory: object, **kw: Any) -> int:  # noqa: ANN401
        kw["on_batch"](7)
        kw["on_batch"](5)
        raise RuntimeError("database went away")

    monkeypatch.setattr(reconciler_module, "purge_expired", failing)
    recon, _ = _purger(h)
    with caplog.at_level("INFO"), pytest.raises(RuntimeError):
        await recon.audit_purge()
    assert added == [7, 5]
    assert caplog.text.count("audit purge batch") == 2


async def test_run_logs_once_that_the_audit_purge_is_disabled_without_a_store(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No audit store: one info line at startup, and the job still skips."""
    h = await Harness({}).start()
    recon, _ = _purger(h, None)
    stop = asyncio.Event()
    stop.set()
    with caplog.at_level("INFO"):
        await recon.run(stop)
    assert caplog.text.count("audit purge disabled: no audit store") == 1


async def test_audit_purge_is_a_no_op_for_zero_retention_and_without_a_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retention 0 never purges; mock mode has no session factory."""
    calls = _patch_purge(monkeypatch, batches=1, per_batch=1)
    zero = await Harness({}, audit_retention_days=0).start()
    assert await _purger(zero)[0].audit_purge() == 0
    mock = await Harness({}).start()
    assert await _purger(mock, None)[0].audit_purge() == 0
    assert calls == []


async def test_audit_purge_is_a_singleton_duty(monkeypatch: pytest.MonkeyPatch) -> None:
    """While another replica holds the `audit-purge` lease, the job does nothing."""
    h = await Harness({}).start()
    calls = _patch_purge(monkeypatch, batches=1, per_batch=1)
    async with h.leases.hold(NIL_TENANT, "_core", "audit-purge"):
        assert await _purger(h)[0].audit_purge() == 0
    assert calls == []
    assert await _purger(h)[0].audit_purge() == 1  # lease free again


async def test_run_schedules_the_audit_purge() -> None:
    """The job is registered with its own interval."""
    h = await Harness({}).start()
    recon, beat = _purger(h)
    stop = asyncio.Event()
    stop.set()
    await recon.run(stop)
    assert "audit-purge" in beat._limits


class FakeDriver:
    """A `DeletionDriver` that answers a script of claims (True) and ends (False) or raises."""

    def __init__(self, *script: bool | Exception) -> None:
        """`script` is consumed in order; once it is empty nothing is runnable."""
        self.script = list(script)
        self.calls = 0

    async def run_once(self) -> bool:
        """The next scripted answer."""
        self.calls += 1
        if not self.script:
            return False
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _deleter(h: Harness, driver: FakeDriver | None) -> tuple[Reconciler, Heartbeat]:
    beat = Heartbeat()
    beat.register("tenant-deletion", 60)
    recon = Reconciler(
        h.settings,
        ReconcilerDeps(
            store=h.store,
            queue=h.queue,
            relay=h.relay,
            hosts=h.hosts,
            leases=h.leases,
            deliveries=None,
            clock=h.clock,
            heartbeat=beat,
            deletion=driver,
        ),
    )
    return recon, beat


async def test_tenant_deletion_runs_claims_until_none_is_runnable_beating_per_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two claims then an empty poll: three passes, a beat before each and one at the end."""
    h = await Harness({}).start()
    driver = FakeDriver(True, True)
    recon, beat = _deleter(h, driver)
    seen: list[str] = []
    monkeypatch.setattr(beat, "beat", seen.append)
    assert await recon.tenant_deletion() == 2
    assert driver.calls == 3
    assert seen == ["tenant-deletion"] * 4


async def test_tenant_deletion_runs_at_most_the_batch_size_per_poll() -> None:
    """`DELETION_BATCH_SIZE` bounds one poll; the rest waits for the next."""
    h = await Harness({}, deletion_batch_size=2).start()
    driver = FakeDriver(True, True, True, True)
    assert await _deleter(h, driver)[0].tenant_deletion() == 2
    assert driver.calls == 2
    assert driver.script == [True, True]


async def test_a_store_error_from_a_pass_is_logged_and_ends_the_poll_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The error does not escape (the loop stays alive) and the next poll runs again."""
    h = await Harness({}).start()
    driver = FakeDriver(True, ConnectionError("database went away"), True)
    recon, _ = _deleter(h, driver)
    with caplog.at_level("ERROR"):
        assert await recon.tenant_deletion() == 1
    assert caplog.text.count("tenant deletion pass failed") == 1
    assert driver.calls == 2
    assert await recon.tenant_deletion() == 1  # the next poll carries on
    assert await recon.tenant_deletion() == 0


async def test_tenant_deletion_is_a_singleton_duty() -> None:
    """While another replica holds the `tenant-deletion` lease, the duty does nothing."""
    h = await Harness({}).start()
    driver = FakeDriver(True)
    async with h.leases.hold(NIL_TENANT, "_core", "tenant-deletion"):
        assert await _deleter(h, driver)[0].tenant_deletion() == 0
    assert driver.calls == 0
    assert await _deleter(h, driver)[0].tenant_deletion() == 1  # lease free again


async def test_tenant_deletion_without_a_driver_is_a_no_op() -> None:
    """No engine composed: nothing to run."""
    h = await Harness({}).start()
    assert await _deleter(h, None)[0].tenant_deletion() == 0


async def test_run_schedules_tenant_deletion_with_a_limit_covering_its_lease_budget(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The loop's stale limit is its interval plus the longer of the two lease TTLs."""
    h = await Harness({}, deletion_lease_ttl_seconds=600).start()
    recon, beat = _deleter(h, None)
    stop = asyncio.Event()
    stop.set()
    with caplog.at_level("INFO"):
        await recon.run(stop)
    assert beat._limits["tenant-deletion"] == h.settings.deletion_poll_seconds + 600
    assert caplog.text.count("tenant deletion disabled: no deletion engine") == 1


async def test_run_logs_nothing_about_deletion_when_a_driver_is_present(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The disabled line is only for a missing driver."""
    h = await Harness({}).start()
    recon, beat = _deleter(h, FakeDriver())
    stop = asyncio.Event()
    stop.set()
    with caplog.at_level("INFO"):
        await recon.run(stop)
    assert "tenant deletion disabled" not in caplog.text
    assert beat._limits["tenant-deletion"] == h.settings.deletion_poll_seconds + max(
        h.settings.lease_ttl_seconds, h.settings.deletion_lease_ttl_seconds
    )


class FakePurger:
    """A `RecordPurger` that reports scripted batches and an unprotected count."""

    def __init__(self, batches: tuple[int, ...] = (), unprotected: int = 0) -> None:
        """`batches` are the per-batch counts handed to `on_batch`."""
        self.batches, self.unprotected, self.calls = batches, unprotected, 0

    async def purge(self, on_batch: Any = None) -> PurgeResult:  # noqa: ANN401
        """Report each batch, then the total."""
        self.calls += 1
        for batch in self.batches:
            if on_batch is not None:
                on_batch(batch)
        return PurgeResult(sum(self.batches), self.unprotected)


def _record_purger(h: Harness, purger: FakePurger | None) -> tuple[Reconciler, Heartbeat]:
    beat = Heartbeat()
    beat.register("deletion-purge", 60)
    recon = Reconciler(
        h.settings,
        ReconcilerDeps(
            store=h.store,
            queue=h.queue,
            relay=h.relay,
            hosts=h.hosts,
            leases=h.leases,
            deliveries=None,
            clock=h.clock,
            heartbeat=beat,
            record_purger=purger,
        ),
    )
    return recon, beat


async def test_deletion_record_purge_beats_per_batch_and_warns_about_unprotected_records(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A beat at the start and one per batch; an unprotected record is logged at WARNING."""
    h = await Harness({}, deletion_retention_days=120).start()
    purger = FakePurger((4, 2), unprotected=3)
    recon, beat = _record_purger(h, purger)
    seen: list[str] = []
    monkeypatch.setattr(beat, "beat", seen.append)
    with caplog.at_level("INFO"):
        assert await recon.deletion_record_purge() == 6
    assert seen == ["deletion-purge"] * 3
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert warnings[0].unprotected_records == 3  # type: ignore[attr-defined]


async def test_deletion_record_purge_does_not_warn_when_every_record_is_protected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No unprotected record, no warning."""
    h = await Harness({}, deletion_retention_days=120).start()
    with caplog.at_level("INFO"):
        assert await _record_purger(h, FakePurger((1,)))[0].deletion_record_purge() == 1
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


async def test_deletion_record_purge_is_a_no_op_for_zero_retention_and_without_a_purger() -> None:
    """The default (0) never purges, and an uncomposed hook does nothing."""
    purger = FakePurger((1,))
    zero = await Harness({}).start()
    assert await _record_purger(zero, purger)[0].deletion_record_purge() == 0
    assert purger.calls == 0
    on = await Harness({}, deletion_retention_days=120).start()
    assert await _record_purger(on, None)[0].deletion_record_purge() == 0


async def test_deletion_record_purge_is_a_singleton_duty() -> None:
    """While another replica holds the `deletion-purge` lease, the duty does nothing."""
    h = await Harness({}, deletion_retention_days=120).start()
    purger = FakePurger((1,))
    async with h.leases.hold(NIL_TENANT, "_core", "deletion-purge"):
        assert await _record_purger(h, purger)[0].deletion_record_purge() == 0
    assert purger.calls == 0
    assert await _record_purger(h, purger)[0].deletion_record_purge() == 1


async def test_run_schedules_the_deletion_record_purge_and_logs_when_it_is_not_composed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The duty has its own interval and limit; no purger composed is one info line."""
    h = await Harness({}).start()
    recon, beat = _record_purger(h, None)
    stop = asyncio.Event()
    stop.set()
    with caplog.at_level("INFO"):
        await recon.run(stop)
    assert beat._limits["deletion-purge"] == (
        h.settings.deletion_purge_interval_seconds + h.settings.lease_ttl_seconds
    )
    assert caplog.text.count("deletion record purge disabled: no record purger") == 1
