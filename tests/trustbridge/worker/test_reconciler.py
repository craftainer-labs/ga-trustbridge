"""The delivery reconciler and retention (0028 invariants 7 and 8)."""

import asyncio
from collections.abc import Sequence
from datetime import timedelta

from tests.trustbridge.fakes import FakeHookClient
from tests.trustbridge.helpers import host
from tests.trustbridge.worker.harness import Harness
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
    await h.store.mark_processed("g-processed", h.clock())
    await h.store.mark_dead("g-dead", "x", h.clock())
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
    real = h.store.existing_ids
    calls = {"n": 0}

    async def existing(delivery_ids: Sequence[str]) -> set[str]:
        calls["n"] += 1
        if calls["n"] == 1:
            return set()  # the batch check ran before the ingress stored it
        await h.add("g-race", relay=False)  # ... which then happens
        return await real(delivery_ids)

    h.store.existing_ids = existing  # type: ignore[method-assign]
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
    await h.store.mark_processed("processed", h.clock())
    await h.store.mark_dead("dead", "x", h.clock())
    recon = _recon(h, None)
    h.advance(8 * 86400)
    await recon.retention()
    processed, dead, pending = [await h.store.get(i) for i in ("processed", "dead", "pending")]
    assert processed is not None
    assert processed.body is None  # purged, but the row remains to dedupe a late redelivery
    assert dead is not None
    assert dead.body is not None  # a dead body stays readable for its longer retention (inv. 7)
    assert pending is not None
    assert pending.body is not None  # a pending row is never touched
    h.advance(23 * 86400)  # 31 days after dead-lettering
    await recon.retention()
    assert await h.store.get("processed") is None
    assert await h.store.get("dead") is None
    assert (await h.store.get("pending")) is not None


async def test_retention_is_a_singleton_duty() -> None:
    """Retention is a singleton duty."""
    h = await Harness({}).start()
    await h.add("d1")
    await h.store.mark_processed("d1", h.clock())
    h.advance(8 * 86400)
    async with h.leases.hold(NIL_TENANT, "_core", "retention"):
        await _recon(h, None).retention()
    assert (await h.store.get("d1")).body is not None  # type: ignore[union-attr]


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
