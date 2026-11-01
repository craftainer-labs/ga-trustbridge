"""The worker: failure counting, dead letter, poison entries, requeue, host availability.

Covers 0028 invariants 3, 4, 7 and 0030 invariant 8.
"""

from datetime import timedelta

import pytest

from tests.trustbridge.worker.harness import Harness, WorkerDied
from trustbridge.worker.lease import LeaseBusy
from trustbridge.worker.worker import HandlerContext


async def test_success_resolves_the_tenant_and_marks_processed() -> None:
    seen: list[HandlerContext] = []

    async def handler(ctx: HandlerContext) -> None:
        seen.append(ctx)

    h = await Harness({"push": handler}).start()
    await h.add("d1")
    assert await h.worker.run_once() == 1
    row = await h.store.get("d1")
    assert row is not None
    assert (row.status, row.attempts) == ("processed", 0)
    assert seen[0].payload == {"installation": {"id": 7}}
    assert seen[0].tenant_id is not None
    assert (await h.valkey.xpending("tb:events", "tb-workers"))["pending"] == 0


async def test_an_event_without_a_handler_is_processed_as_a_no_op() -> None:
    h = await Harness({}).start()
    await h.add("d1", event="ping")
    await h.worker.run_once()
    assert (await h.store.get("d1")).status == "processed"  # type: ignore[union-attr]


async def test_the_failure_counter_increases_only_on_handler_failures_with_backoff() -> None:  # 3
    outcomes = iter([RuntimeError("boom"), RuntimeError("boom"), None])

    async def handler(ctx: HandlerContext) -> None:
        if (error := next(outcomes)) is not None:
            raise error

    h = await Harness({"push": handler}).start()
    await h.add("d1")
    await h.worker.run_once()
    first = await h.store.get("d1")
    assert first is not None
    assert (first.status, first.attempts) == ("pending", 1)
    assert first.last_error == "RuntimeError: boom"
    assert first.next_attempt_at == h.clock.now + timedelta(seconds=5)
    assert await h.relay.tick() == 0  # not due yet: no re-add
    h.advance(5)
    assert await h.relay.tick() == 1  # due: the retry re-add
    await h.worker.run_once()
    second = await h.store.get("d1")
    assert second is not None
    assert second.attempts == 2
    h.advance(10)  # the second backoff is 10 s
    assert await h.relay.tick() == 1
    await h.worker.run_once()
    final = await h.store.get("d1")
    assert final is not None
    assert (final.status, final.attempts) == ("processed", 2)


async def test_exhausting_the_attempts_dead_letters_with_no_body() -> None:  # inv. 3, 4, 7
    async def handler(ctx: HandlerContext) -> None:
        raise RuntimeError("always")

    h = await Harness({"push": handler}).start()
    await h.add("d1")
    for _ in range(3):
        await h.worker.run_once()
        h.advance(900)
        await h.relay.tick()
    row = await h.store.get("d1")
    assert row is not None
    assert (row.status, row.attempts) == ("dead", 3)
    assert row.body is not None  # readable from Postgres until its dead retention elapses
    dead = h.valkey.entries("tb:events:dead")
    assert [fields for _, fields in dead] == [{"delivery_id": "d1", "error": "RuntimeError: always"}]
    assert await h.relay.tick() == 0  # a dead row is never relayed again


async def test_relay_duplicates_and_re_relays_never_increase_the_counter() -> None:  # inv. 3
    h = await Harness({}).start()
    await h.add("d1")
    await h.queue.add("d1")  # a relay duplicate
    h.advance(1)
    assert await h.relay.resweep(0) == 1  # and a re-relay sweep
    while await h.worker.run_once():
        pass
    row = await h.store.get("d1")
    assert row is not None
    assert (row.status, row.attempts) == ("processed", 0)


async def test_a_busy_lease_defers_without_counting() -> None:  # inv. 3
    async def handler(ctx: HandlerContext) -> None:
        raise LeaseBusy("adapter/key")

    h = await Harness({"push": handler}).start()
    await h.add("d1")
    await h.worker.run_once()
    row = await h.store.get("d1")
    assert row is not None
    assert (row.status, row.attempts) == ("pending", 0)
    assert row.next_attempt_at is not None
    assert (await h.valkey.xpending("tb:events", "tb-workers"))["pending"] == 0


async def test_a_host_that_is_not_ready_is_neither_handled_counted_nor_dead_lettered() -> None:
    called: list[str] = []

    async def handler(ctx: HandlerContext) -> None:
        called.append(ctx.delivery.delivery_id)

    h = await Harness({"push": handler}, failing_hosts={"bad"}).start()  # 3, 4, 0030-8
    await h.add("d1", host_name="bad")
    for _ in range(10):  # far more deliveries than MAX_DELIVERY_ATTEMPTS
        await h.relay.resweep(0)
        await h.worker.run_once()
        h.advance(120)
    row = await h.store.get("d1")
    assert row is not None
    assert (row.status, row.attempts) == ("pending", 0)
    assert called == []
    assert h.valkey.entries("tb:events:dead") == []
    assert (await h.valkey.xpending("tb:events", "tb-workers"))["pending"] == 0  # acked, unclaimed


async def test_a_host_becoming_ready_re_adds_its_waiting_rows_without_the_periodic_sweep() -> None:
    called: list[str] = []

    async def handler(ctx: HandlerContext) -> None:
        called.append(ctx.delivery.delivery_id)

    h = await Harness({"push": handler}, failing_hosts={"bad"}).start()  # 0030 invariant 8
    await h.add("d1", host_name="bad")
    await h.add("d2", host_name="ok")
    await h.worker.run_once()
    assert called == ["d2"]
    h.checker.failing.clear()
    await h.hosts.check_all(only_not_ready=True)  # the recheck: bad -> ready fires the listener
    await h.worker.run_once()
    assert called == ["d2", "d1"]
    assert (await h.store.get("d1")).status == "processed"  # type: ignore[union-attr]


async def test_a_worker_that_keeps_dying_dead_letters_by_stream_delivery_count() -> None:  # inv. 4
    calls: list[str] = []

    async def handler(ctx: HandlerContext) -> None:
        calls.append(ctx.delivery.delivery_id)
        raise WorkerDied

    h = await Harness({"push": handler}).start()  # MAX_DELIVERY_ATTEMPTS = 3
    await h.add("d1")
    original = h.valkey.entries("tb:events")[0][0]
    for _ in range(3):
        with pytest.raises(WorkerDied):
            await h.worker.run_once()
        h.advance(61)  # past the claim idle time
    assert await h.queue.delivery_count(original) == 3
    # a relay duplicate / retry re-add is a new entry: it is not a delivery of the original
    duplicate = await h.queue.add("d1")
    assert [e.entry_id for e in await h.queue.read("other", count=10, block_ms=0)] == [duplicate]
    assert await h.queue.delivery_count(duplicate) == 1
    assert await h.queue.delivery_count(original) == 3
    await h.queue.ack(duplicate)
    await h.worker.run_once()  # claims the original a 4th time (> MAX): dead letter, no handling
    assert len(calls) == 3
    row = await h.store.get("d1")
    assert row is not None
    assert (row.status, row.attempts) == ("dead", 0)  # no failure was ever recorded
    assert [f["delivery_id"] for _, f in h.valkey.entries("tb:events:dead")] == ["d1"]


async def test_requeue_makes_exactly_that_row_deliverable_with_a_reset_counter() -> None:  # inv. 7
    mode = {"fail": True}

    async def handler(ctx: HandlerContext) -> None:
        if mode["fail"]:
            raise RuntimeError("down")

    h = await Harness({"push": handler}).start()
    for delivery_id in ("d1", "d2"):
        await h.add(delivery_id)
    for _ in range(3):
        await h.worker.run_once()
        h.advance(900)
        await h.relay.tick()
    assert {(await h.store.get(i)).status for i in ("d1", "d2")} == {"dead"}  # type: ignore[union-attr]
    mode["fail"] = False
    assert await h.store.requeue("d1") is True
    row = await h.store.get("d1")
    assert row is not None
    assert (row.status, row.attempts, row.last_error) == ("pending", 0, None)
    assert (await h.store.get("d2")).status == "dead"  # type: ignore[union-attr]
    assert await h.relay.tick() == 1
    await h.worker.run_once()
    assert (await h.store.get("d1")).status == "processed"  # type: ignore[union-attr]
    assert await h.store.requeue("d1") is False  # only a dead row can be requeued
    assert await h.store.requeue("missing") is False


async def test_requeue_refuses_a_row_whose_body_was_purged() -> None:  # inv. 7
    h = await Harness({}).start()
    await h.add("d1")
    await h.store.mark_dead("d1", "x", h.clock())
    h.advance(31 * 86400)
    assert await h.store.purge_bodies(h.clock(), h.clock()) == 1
    assert await h.store.requeue("d1") is False


async def test_infrastructure_errors_re_add_the_entry_without_counting() -> None:
    h = await Harness({"push": lambda ctx: None}).start()  # type: ignore[dict-item]
    await h.add("d1")
    real_get = h.store.get
    boom = {"on": True}

    async def flaky_get(delivery_id: str):  # type: ignore[no-untyped-def]  # noqa: ANN202
        if boom["on"]:
            boom["on"] = False
            raise ConnectionError("postgres down")
        return await real_get(delivery_id)

    h.store.get = flaky_get  # type: ignore[method-assign]
    await h.worker.run_once()
    row = await real_get("d1")
    assert row is not None
    assert row.attempts == 0
    assert len(h.valkey.entries("tb:events")) == 2  # re-added as a fresh entry
    assert (await h.valkey.xpending("tb:events", "tb-workers"))["pending"] == 0
