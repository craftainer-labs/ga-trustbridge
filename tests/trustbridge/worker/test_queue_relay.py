"""Relay sweep after stream loss, safe trimming and the lag metric (0028 invariants 5 and 6)."""

from datetime import timedelta

import pytest

from tests.trustbridge.fakes import FakeRedis
from tests.trustbridge.helpers import as_redis
from tests.trustbridge.worker.harness import Harness
from trustbridge.worker import metrics
from trustbridge.worker.queue import StreamQueue
from trustbridge.worker.reconciler import Reconciler, ReconcilerDeps


async def test_the_resweep_after_stream_loss_re_adds_every_live_row() -> None:  # inv. 5
    """The resweep after stream loss re adds every live row."""
    h = await Harness({}).start()
    for delivery_id in ("d1", "d2", "d3", "d4", "d5"):
        await h.add(delivery_id, relay=False)
    await h.relay.relay_ids(["d1", "d2", "d3", "d5"])  # d4 was never relayed
    await h.store.mark_processed("d2", h.clock())
    await h.store.defer("d3", h.clock() + timedelta(hours=1))
    await h.store.mark_dead("d5", "x", h.clock())
    await h.valkey.flushall()  # restart without persistence / flush / failover
    await h.queue.ensure_group()
    h.advance(1)
    assert await h.relay.resweep(0) == 2
    assert sorted(h.valkey.ids_of("tb:events")) == ["d1", "d4"]  # not processed, dead or delayed
    h.advance(3600)  # the delayed retry is now due
    await h.relay.resweep(0)
    assert "d3" in h.valkey.ids_of("tb:events")


async def test_a_delayed_retry_waits_for_its_time_and_is_then_re_added_once_by_the_tick() -> None:
    """A delayed retry waits for its time and is then re added once by the tick."""
    h = await Harness({}).start()
    await h.add("d1")
    soon = h.clock() + timedelta(seconds=30)
    await h.store.record_failure("d1", "e", h.clock(), soon)
    assert await h.relay.tick() == 0
    h.advance(30)
    assert await h.relay.tick() == 1
    assert await h.relay.tick() == 0  # relayed since it was scheduled


async def _filled(n: int) -> tuple[FakeRedis, StreamQueue]:
    valkey = FakeRedis()
    queue = StreamQueue(as_redis(valkey))
    await queue.ensure_group()
    for i in range(n):
        valkey.advance(1)
        await queue.add(f"d{i}")
    return valkey, queue


async def test_trim_keeps_unread_and_pending_entries() -> None:  # inv. 6
    """Trim keeps unread and pending entries."""
    valkey, queue = await _filled(5)
    first, second = await queue.read("c", count=2, block_ms=0)
    await queue.ack(first.entry_id)  # `second` stays pending; three entries are unread
    assert await queue.trim_events() == 1
    assert valkey.ids_of("tb:events") == ["d1", "d2", "d3", "d4"]
    assert second.entry_id in [e for e, _ in valkey.entries("tb:events")]


async def test_trim_with_nothing_pending_still_keeps_unread_entries() -> None:  # inv. 6
    """Trim with nothing pending still keeps unread entries."""
    valkey, queue = await _filled(4)
    for entry in await queue.read("c", count=2, block_ms=0):
        await queue.ack(entry.entry_id)
    await queue.trim_events()
    assert valkey.ids_of("tb:events") == ["d1", "d2", "d3"]  # d2 is the last delivered; d3 unread


async def test_trim_keeps_everything_while_the_group_is_stalled() -> None:  # inv. 6
    """Trim keeps everything while the group is stalled."""
    valkey, queue = await _filled(4)
    assert await queue.trim_events() == 0
    assert len(valkey.ids_of("tb:events")) == 4


async def test_trim_keeps_an_old_pending_entry_behind_newer_acknowledged_ones() -> None:  # inv. 6
    """Trim keeps an old pending entry behind newer acknowledged ones."""
    valkey, queue = await _filled(4)
    entries = await queue.read("c", count=3, block_ms=0)
    await queue.ack(entries[1].entry_id)
    await queue.ack(entries[2].entry_id)  # entries[0] is still pending
    assert await queue.trim_events() == 0
    assert len(valkey.ids_of("tb:events")) == 4


async def test_trim_without_a_group_or_a_stream_removes_nothing() -> None:  # inv. 6
    """Trim without a group or a stream removes nothing."""
    valkey = FakeRedis()
    queue = StreamQueue(as_redis(valkey))
    assert await queue.trim_events() == 0  # no stream at all
    await valkey.xadd("tb:events", {"id": "d0"})
    assert await queue.trim_events() == 0  # a stream, but no consumer group yet
    assert valkey.ids_of("tb:events") == ["d0"]


async def test_dead_letter_stream_is_trimmed_by_age() -> None:
    """Dead letter stream is trimmed by age."""
    valkey = FakeRedis()
    queue = StreamQueue(as_redis(valkey))
    await queue.dead_letter("old", "e")
    valkey.advance(10_000)
    await queue.dead_letter("new", "e")
    assert await queue.trim_dead(valkey.now_ms - 5_000) == 1
    assert [f["delivery_id"] for _, f in valkey.entries("tb:events:dead")] == ["new"]
    assert await queue.trim_dead(0) == 0


async def test_group_lag_is_exported_as_a_metric(monkeypatch: pytest.MonkeyPatch) -> None:  # inv. 6
    """Group lag is exported as a metric."""
    h = await Harness({}).start()
    for delivery_id in ("d1", "d2", "d3"):
        await h.add(delivery_id)
    recorded: list[int | None] = []
    monkeypatch.setattr(metrics, "set_stream_lag", recorded.append)
    reconciler = Reconciler(
        h.settings,
        ReconcilerDeps(
            store=h.store, queue=h.queue, relay=h.relay, hosts=h.hosts, leases=h.leases,
            deliveries=None, clock=h.clock,
        ),
    )
    await reconciler.trim_streams()
    assert recorded == [3]
    assert await h.queue.lag() == 3


async def test_lag_is_none_when_the_server_does_not_report_it() -> None:
    """Lag is none when the server does not report it."""
    queue = StreamQueue(as_redis(FakeRedis()))
    assert await queue.lag() is None
