"""Worker runtime edges: stream parsing, lease keepalive, loops, reconciler errors, bootstrap."""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from redis.exceptions import ResponseError

import app.db as app_db
from tests.trustbridge.fakes import FakeHookClient, FakeHostChecker, FakeRedis
from tests.trustbridge.helpers import as_redis, make_settings
from tests.trustbridge.worker.harness import Harness, key
from tests.trustbridge.worker.test_reconciler import _hd, _recon, _reconciler
from trustbridge.worker import metrics, migrate, runtime
from trustbridge.worker.deliveries import DeliveryKey, InMemoryDeliveryStore, NewDelivery
from trustbridge.worker.lease import InMemoryLeaseStore, LeaseLost, LeaseManager
from trustbridge.worker.queue import StreamEntry, StreamQueue, _entries
from trustbridge.worker.reconciler import periodic
from trustbridge.worker.runtime import ProcessOverrides, open_redis, session_factory_or_none

TENANT = uuid.uuid4()


def test_stream_replies_are_normalised_from_either_protocol_shape() -> None:
    """RESP3 dicts, empty fields and entries without an `id` are all handled."""
    resp3 = {
        "tb:events": [
            [("1-0", {"id": "a", "host": "ok"}), ("2-0", {})],
            ("3-0", {"other": "x"}),
            ("5-0", {"id": "no-host"}),
        ]
    }
    orphans: list[str] = []
    assert _entries(resp3, orphans) == [StreamEntry("1-0", key("a"))]
    assert orphans == ["3-0", "5-0"]  # no `id` / no `host`: old format, to be acked
    raw = [["tb:events", [(b"4-0", {b"id": b"b", b"host": b"ok"})]]]
    assert _entries(raw, []) == [StreamEntry("4-0", key("b"))]
    assert _entries(None, []) == []


class _NoGroupRedis(FakeRedis):
    """A server whose group creation fails for a reason other than `BUSYGROUP`."""

    async def xgroup_create(self, *_a: Any, **_k: Any) -> bool:  # noqa: ANN401
        """Fail like a wrong-type key."""
        raise ResponseError("WRONGTYPE nope")


async def test_ensure_group_only_swallows_busygroup() -> None:
    """Any other server error is raised."""
    with pytest.raises(ResponseError, match="WRONGTYPE"):
        await StreamQueue(as_redis(_NoGroupRedis())).ensure_group()


class _PendingWithoutMin(FakeRedis):
    """A group with pending entries whose summary has no minimum id."""

    async def xinfo_groups(self, name: str) -> list[dict[str, Any]]:
        """One group with a pending entry."""
        return [{"name": "g", "pending": 1, "last-delivered-id": "5-0"}]

    async def xpending(self, name: str, groupname: str) -> dict[str, Any]:
        """A summary with no minimum."""
        return {"pending": 1, "min": None}


async def test_trimming_without_a_pending_minimum_uses_the_last_delivered_id() -> None:
    """The boundary falls back to the last delivered id."""
    fake = _PendingWithoutMin()
    fake.streams["tb:events"] = [("1-0", {"id": "a"}), ("6-0", {"id": "b"})]
    assert await StreamQueue(as_redis(fake)).trim_events() == 1


def test_the_lag_gauge_reports_only_a_known_lag() -> None:
    """The observable gauge yields nothing until a lag is set, and again after it is cleared."""
    metrics.set_stream_lag(7)
    assert [o.value for o in metrics._observe_lag(None)] == [7]  # type: ignore[arg-type]
    metrics.set_stream_lag(None)
    assert list(metrics._observe_lag(None)) == []  # type: ignore[arg-type]


async def test_the_migrate_job_runs_alembic_upgrade_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_alembic_upgrade` upgrades to head; `main upgrade` runs the locked job."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "alembic.command.upgrade", lambda cfg, rev: calls.append((cfg.config_file_name or "", rev))
    )
    migrate._alembic_upgrade()
    assert calls == [("alembic.ini", "head")]

    async def fake_upgrade(_settings: object) -> int:
        return 0

    monkeypatch.setattr(migrate, "run_upgrade", fake_upgrade)
    monkeypatch.setattr(migrate, "load_trustbridge_settings", make_settings)
    assert await asyncio.to_thread(migrate.main, ["upgrade"]) == 0


async def test_a_delivery_reconciler_with_nothing_failed_redelivers_nothing() -> None:
    """Only successful attempts: nothing to do."""
    h = await Harness({}).start()
    client = FakeHookClient([_hd(1, "g1", 200, h)])
    assert await _reconciler(h, client).reconcile_host(h.settings.github_hosts[0]) == 0


async def test_failing_jobs_and_failing_host_reconciliation_are_logged_not_raised() -> None:
    """One bad job or host never stops the reconciler."""
    stop = asyncio.Event()

    async def bad_job() -> None:
        stop.set()
        raise RuntimeError("boom")

    await periodic("bad", 0.001, bad_job, stop)

    class Exploding(FakeHookClient):
        def list_deliveries(self, host: Any, *, since: datetime) -> Any:  # noqa: ANN401
            raise RuntimeError("github down")

    h = await Harness({}).start()
    assert await _recon(h, Exploding([])).reconcile_deliveries() == 0


async def test_relaying_with_a_dead_stream_marks_nothing() -> None:
    """When the first add fails nothing is stamped as relayed."""
    h = await Harness({}).start()
    await h.store.insert(NewDelivery("d1", "ok", "push", {}, b"{}", h.clock()))
    h.valkey.down = True
    with pytest.raises(ConnectionError):
        await h.relay.relay_keys([key("d1")])
    row = await h.store.get(key("d1"))
    assert row is not None
    assert row.last_relayed_at is None


async def test_the_memory_store_deduplicates_keys_and_bodies_per_host() -> None:
    """The same `(host, id)` or the same body on one host is a duplicate; other hosts are not."""
    store = InMemoryDeliveryStore()
    now = datetime.now(UTC)
    assert await store.insert(NewDelivery("d1", "h", "push", {}, b"a", now)) is True
    assert await store.insert(NewDelivery("d1", "h", "push", {}, b"b", now)) is False
    assert await store.insert(NewDelivery("d2", "h", "push", {}, b"a", now)) is False  # replay
    assert await store.insert(NewDelivery("d1", "h2", "push", {}, b"a", now)) is True
    assert sorted(store.rows) == [
        DeliveryKey("h", "d1"),
        DeliveryKey("h", "d2"),
        DeliveryKey("h2", "d1"),
    ]
    marker = store.rows[DeliveryKey("h", "d2")]  # the replay keeps a terminal row, hash NULL
    assert (marker.status, marker.body, marker.body_sha256) == ("processed", None, None)
    assert marker.last_error == "duplicate body of delivery d1"
    assert await store.existing_keys([marker.key]) == {marker.key}  # the reconciler skips it
    assert await store.insert(NewDelivery("d2", "h", "push", {}, b"zzz", now)) is False
    assert await store.insert(NewDelivery("d3", "h", "push", {}, b"a", now)) is False


async def test_the_memory_store_ignores_unknown_ids_and_settled_rows() -> None:
    """`mark_relayed` skips unknown ids; `mark_processed` leaves a dead row dead."""
    store = InMemoryDeliveryStore()
    now = datetime.now(UTC)
    await store.insert(NewDelivery("d1", "h", "push", {}, b"{}", now))
    other = DeliveryKey("h", "d1")
    await store.mark_relayed([DeliveryKey("h", "missing"), other], now)
    await store.mark_dead(other, "x", now)
    await store.mark_processed(other, now)
    row = await store.get(other)
    assert row is not None
    assert (row.status, row.last_relayed_at) == ("dead", now)


async def test_the_worker_loop_backs_off_after_a_stream_error_and_survives_a_failed_readd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing read logs and sleeps; a failing re-add after an infrastructure error is logged."""
    h = await Harness({}).start()
    stop = asyncio.Event()

    async def failing_read() -> int:
        stop.set()
        raise ConnectionError("valkey down")

    slept: list[float] = []

    async def no_sleep(seconds: float) -> None:
        slept.append(seconds)

    h.worker.run_once = failing_read  # type: ignore[method-assign]
    monkeypatch.setattr("trustbridge.worker.worker.asyncio.sleep", no_sleep)
    await h.worker.run(stop)
    assert slept == [1.0]

    async def broken(entry: StreamEntry, *, from_pending: bool) -> None:
        raise RuntimeError("postgres down")

    h.worker._process = broken  # type: ignore[method-assign]
    h.valkey.down = True  # the re-add fails too
    await h.worker.process(StreamEntry("1-0", key("d1")), from_pending=False)


async def test_the_keepalive_survives_a_failing_renew_and_cancellation_is_not_swallowed() -> None:
    """A renew error is logged and retried; an outside cancellation propagates."""
    store = InMemoryLeaseStore()
    manager = LeaseManager(store, ttl_seconds=0.06)
    real_renew = store.renew
    calls = {"n": 0}

    async def flaky(lease: Any, ttl: float) -> bool:  # noqa: ANN401
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("db blip")
        return await real_renew(lease, ttl)

    store.renew = flaky  # type: ignore[method-assign]
    async with manager.hold(TENANT, "a", "k"):
        await asyncio.sleep(0.15)
    assert calls["n"] >= 2

    async def body() -> None:
        async with manager.hold(TENANT, "a", "other"):
            await asyncio.sleep(10)

    task = asyncio.create_task(body())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_lost_lease_surfaces_as_lease_lost_and_skips_the_release() -> None:
    """When the renew finds the lease gone the body is cancelled and `LeaseLost` is raised."""
    store = InMemoryLeaseStore()
    manager = LeaseManager(store, ttl_seconds=0.06)

    async def gone(lease: Any, ttl: float) -> bool:  # noqa: ANN401
        return False

    store.renew = gone  # type: ignore[method-assign]
    with pytest.raises(LeaseLost):
        async with manager.hold(TENANT, "a", "k"):
            await asyncio.sleep(10)


async def test_the_bootstrap_helpers_build_real_clients_lazily() -> None:
    """`session_factory_or_none` is None in mock mode; `open_redis` builds a lazy client."""
    assert session_factory_or_none(make_settings()) is None
    assert session_factory_or_none(make_settings(mode="dev", allow_mock_mode=False)) is (
        app_db.session_factory
    )
    client = open_redis(make_settings())
    await client.aclose()


async def test_run_process_opens_and_closes_its_own_valkey_client(
    monkeypatch: pytest.MonkeyPatch, key_signers: None
) -> None:
    """Without an injected client the process opens one and closes it on exit."""
    closed: list[bool] = []

    class Owned(FakeRedis):
        async def aclose(self) -> None:
            closed.append(True)

    monkeypatch.setattr(runtime, "open_redis", lambda _settings: as_redis(Owned()))
    stop = asyncio.Event()
    stop.set()
    code = await runtime.run_process(
        "worker", make_settings(), ProcessOverrides(stop=stop, checker=FakeHostChecker())
    )
    assert code == 0
    assert closed == [True]
