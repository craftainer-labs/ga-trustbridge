"""Bounding authz.denied rows (NG6d decision A6): burst, summary on window close, restarts."""

import asyncio
import logging
from dataclasses import replace

import pytest

from tests.trustbridge.fakes import FakeRedis
from tests.trustbridge.helpers import as_redis
from trustbridge.audit.denial import Denial
from trustbridge.audit.throttle import PENDING_KEY, ThrottledDenials
from trustbridge.ratelimit import InMemoryWindowCounter, ValkeyWindowCounter, WindowCounter

WINDOW = 60
BURST = 5


class Sink:
    """Records what reaches the audit sink."""

    def __init__(self) -> None:
        """Start empty."""
        self.rows: list[Denial] = []

    async def denied(self, denial: Denial) -> None:
        """Record."""
        self.rows.append(denial)


class Clock:
    """A settable clock."""

    def __init__(self, now: float = 6000.0) -> None:
        """Start at the beginning of a window."""
        self.now = now

    def __call__(self) -> float:
        """The time."""
        return self.now


class Metric:
    """A counter stand-in."""

    def __init__(self) -> None:
        """Start at zero."""
        self.total = 0

    def add(self, amount: int) -> None:
        """Count."""
        self.total += amount


def _denial(
    sub: str = "mallory", reason: str = "tenant_not_granted", resource: str = "x"
) -> Denial:
    return Denial(
        sub=sub,
        iss="https://idp",
        reason=reason,  # type: ignore[arg-type]
        required_role="viewer",
        scope="tenant",
        tenant_id=resource,
        source_ip=None,
        request_id=None,
    )


def _throttle(
    sink: Sink, counter: WindowCounter, clock: Clock, metric: Metric | None = None
) -> ThrottledDenials:
    return ThrottledDenials(
        sink,
        counter,
        window_seconds=WINDOW,
        burst=BURST,
        timeout_ms=50,
        suppressed=metric,
        clock=clock,
    )


async def test_the_fifth_denial_is_recorded_the_sixth_is_only_counted() -> None:
    """The boundary: burst of 5 per (subject, reason) and window."""
    sink, metric = Sink(), Metric()
    throttle = _throttle(sink, InMemoryWindowCounter(), Clock(), metric)
    for n in range(5):
        await throttle.denied(_denial(resource=f"t{n}"))
    assert [r.tenant_id for r in sink.rows] == ["t0", "t1", "t2", "t3", "t4"]
    assert metric.total == 0
    await throttle.denied(_denial(resource="t5"))
    await throttle.denied(_denial(resource="t6"))
    assert len(sink.rows) == 5
    assert metric.total == 2


async def test_subjects_and_reasons_have_their_own_windows() -> None:
    """The key is (subject, reason)."""
    sink = Sink()
    throttle = _throttle(sink, InMemoryWindowCounter(), Clock())
    for _ in range(7):
        await throttle.denied(_denial())
    await throttle.denied(_denial(sub="other"))
    await throttle.denied(_denial(reason="role_missing"))
    assert len(sink.rows) == 5 + 2


async def test_the_next_denial_after_the_window_writes_one_summary_then_records() -> None:
    """Window close, path (a): one summary with the suppressed count, then the new window."""
    sink, clock = Sink(), Clock()
    throttle = _throttle(sink, InMemoryWindowCounter(), clock)
    for _ in range(9):
        await throttle.denied(_denial())
    assert len(sink.rows) == 5
    clock.now += WINDOW
    await throttle.denied(_denial(resource="fresh"))
    fresh, summary = sink.rows[5:]  # the current denial first, then the old window's summary
    assert (summary.suppressed, summary.window_seconds, summary.sub) == (4, WINDOW, "mallory")
    assert summary.window == 100
    assert fresh.suppressed == 0
    assert fresh.tenant_id == "fresh"
    await throttle.flush()  # nothing left to write
    assert len(sink.rows) == 7


async def test_a_window_that_closes_with_zero_suppressed_writes_no_summary() -> None:
    """Exactly the burst: nothing was dropped, so nothing is summarised."""
    sink, clock = Sink(), Clock()
    throttle = _throttle(sink, InMemoryWindowCounter(), clock)
    for _ in range(5):
        await throttle.denied(_denial())
    clock.now += WINDOW
    await throttle.denied(_denial())
    assert await throttle.flush() == 0
    assert [r.suppressed for r in sink.rows] == [0] * 6


async def test_flush_writes_the_summary_without_another_denial() -> None:
    """Path (b): the periodic flush closes the window of a subject that stopped."""
    sink, clock = Sink(), Clock()
    throttle = _throttle(sink, InMemoryWindowCounter(), clock)
    for _ in range(8):
        await throttle.denied(_denial())
    assert await throttle.flush() == 0  # the window is still open
    clock.now += WINDOW
    assert await throttle.flush() == 1
    assert sink.rows[-1].suppressed == 3
    assert await throttle.flush() == 0  # once


async def test_two_replicas_write_one_summary_between_them() -> None:
    """The claim marker makes the summary happen once across replicas and paths."""
    valkey = FakeRedis()
    sink, clock = Sink(), Clock()
    one = _throttle(sink, ValkeyWindowCounter(as_redis(valkey)), clock)
    two = _throttle(sink, ValkeyWindowCounter(as_redis(valkey)), clock)
    for throttle in (one, two, one, two, one, two, one, two):
        await throttle.denied(_denial())
    assert len(sink.rows) == 5
    clock.now += WINDOW
    written = await one.flush() + await two.flush()
    assert written == 1
    assert [r.suppressed for r in sink.rows].count(3) == 1


async def test_a_counter_store_failure_records_the_row() -> None:
    """Fail open to completeness: the audit trail is not thinned by an outage."""
    valkey = FakeRedis()
    valkey.down = True
    sink = Sink()
    throttle = _throttle(sink, ValkeyWindowCounter(as_redis(valkey)), Clock())
    for _ in range(8):
        await throttle.denied(_denial())
    assert len(sink.rows) == 8


async def test_the_summary_keeps_the_sample_but_not_the_resource() -> None:
    """A summary names the role and scope of the last suppressed denial."""
    sink, clock = Sink(), Clock()
    throttle = _throttle(sink, InMemoryWindowCounter(), clock)
    for _ in range(6):
        await throttle.denied(replace(_denial(), required_role="admin"))
    clock.now += WINDOW
    await throttle.flush()
    assert (sink.rows[-1].required_role, sink.rows[-1].suppressed) == ("admin", 1)


async def test_a_flush_that_cannot_reach_the_counter_is_logged_and_skipped() -> None:
    """The entry is dropped; the count stays in the store for the next denial."""
    valkey = FakeRedis()
    sink, clock = Sink(), Clock()
    throttle = _throttle(sink, ValkeyWindowCounter(as_redis(valkey)), clock)
    for _ in range(7):
        await throttle.denied(_denial())
    clock.now += WINDOW
    valkey.down = True
    assert await throttle.flush() == 0
    valkey.down = False
    await throttle.denied(_denial())
    assert sink.rows[6].suppressed == 2


async def test_a_gap_of_several_windows_still_summarises_the_old_window() -> None:
    """Path (a) summarises every closed pending window, however long ago."""
    sink, clock = Sink(), Clock()
    throttle = _throttle(sink, InMemoryWindowCounter(), clock)
    for _ in range(9):
        await throttle.denied(_denial())
    clock.now += 3 * WINDOW
    await throttle.denied(_denial(resource="fresh"))
    fresh, summary = sink.rows[5:]
    assert (summary.suppressed, fresh.tenant_id, fresh.suppressed) == (4, "fresh", 0)


async def test_every_closed_window_of_the_pair_gets_its_own_summary() -> None:
    """Two windows left pending by a failing sink are both written by the next denial."""
    sink, clock = _FlakySink(1), Clock()
    counter = InMemoryWindowCounter()
    throttle = _throttle(sink, counter, clock)
    for _ in range(8):  # 3 suppressed in window 100
        await throttle.denied(_denial())
    clock.now += WINDOW
    await throttle.denied(_denial())  # the summary of 100 fails: it stays pending
    for _ in range(9):  # window 101: 10 denials in all, 5 suppressed
        await throttle.denied(_denial())
    clock.now += 4 * WINDOW
    await throttle.denied(_denial(resource="later"))
    assert [(r.window, r.suppressed) for r in sink.rows if r.suppressed] == [(100, 3), (101, 5)]
    assert await throttle.flush() == 0


async def test_a_restarted_process_finishes_the_work_from_the_shared_store() -> None:
    """A new instance has no memory of the old one; its flush still writes the summary."""
    valkey = FakeRedis()
    sink, clock = Sink(), Clock()
    before = _throttle(sink, ValkeyWindowCounter(as_redis(valkey)), clock)
    for n in range(7):
        await before.denied(_denial(resource=f"t{n}"))
    clock.now += WINDOW
    after = _throttle(sink, ValkeyWindowCounter(as_redis(valkey)), clock)  # restarted process
    assert await after.flush() == 1
    summary = sink.rows[-1]
    assert (summary.suppressed, summary.sub, summary.tenant_id) == (2, "mallory", "t6")
    assert await after.flush() == 0
    assert await before.flush() == 0


async def test_the_open_window_is_never_summarised_early() -> None:
    """A flush inside the window leaves it pending, so later denials still count."""
    sink, clock = Sink(), Clock()
    throttle = _throttle(sink, InMemoryWindowCounter(), clock)
    for _ in range(7):
        await throttle.denied(_denial())
    assert await throttle.flush() == 0
    for _ in range(3):
        await throttle.denied(_denial())
    clock.now += WINDOW
    assert await throttle.flush() == 1
    assert sink.rows[-1].suppressed == 5


class _SlowSink(Sink):
    """A sink that yields once per write, so racing flushes interleave."""

    async def denied(self, denial: Denial) -> None:
        """Yield, then record."""
        await asyncio.sleep(0)
        await super().denied(denial)


async def test_two_replicas_racing_write_the_same_summary_twice_and_it_is_recognisable() -> None:
    """At-least-once: a duplicate carries the same (sub, reason, window); nothing is lost."""
    valkey = FakeRedis()
    sink, clock = _SlowSink(), Clock()
    one = _throttle(sink, ValkeyWindowCounter(as_redis(valkey)), clock)
    two = _throttle(sink, ValkeyWindowCounter(as_redis(valkey)), clock)
    for _ in range(9):
        await one.denied(_denial())
    clock.now += WINDOW
    await asyncio.gather(one.flush(), two.flush())
    rows = [r for r in sink.rows if r.suppressed]
    assert [(r.sub, r.reason, r.window, r.suppressed) for r in rows] == [
        ("mallory", "tenant_not_granted", 100, 4)
    ] * 2
    assert (await one.flush(), await two.flush()) == (0, 0)  # done, and out of the index
    assert valkey.sets.get("tb:audit:denied:pending:" + one._base(_denial())) is None


async def test_the_summary_describes_the_last_suppressed_denial() -> None:
    """The row's sample is the last suppressed denial, with its address and request id."""
    sink, clock = Sink(), Clock()
    throttle = _throttle(sink, InMemoryWindowCounter(), clock)
    for n in range(7):
        await throttle.denied(replace(_denial(resource=f"t{n}"), source_ip=f"192.0.2.{n}"))
    clock.now += WINDOW
    await throttle.flush()
    assert (sink.rows[-1].tenant_id, sink.rows[-1].source_ip) == ("t6", "192.0.2.6")


class _Counting(InMemoryWindowCounter):
    """Counts the calls a denial costs."""

    def __init__(self) -> None:
        """Start at zero."""
        super().__init__()
        self.calls: list[str] = []

    async def incr(self, key: str, ttl_seconds: int) -> int:
        """Count."""
        self.calls.append("incr")
        return await super().incr(key, ttl_seconds)

    async def note(self, text_key: str, text: str, sets: dict[str, str], ttl_seconds: int) -> None:
        """Count."""
        self.calls.append("note")
        await super().note(text_key, text, sets, ttl_seconds)


async def test_only_suppressed_denials_note_the_window_and_in_one_call() -> None:
    """Burst denials cost one call (the count); each suppressed one costs two (count and note)."""
    counter = _Counting()
    throttle = _throttle(Sink(), counter, Clock())
    for _ in range(BURST):
        await throttle.denied(_denial())
    assert counter.calls == ["incr"] * BURST
    await throttle.denied(_denial())
    await throttle.denied(_denial())
    assert counter.calls[BURST:] == ["incr", "note", "incr", "note"]


class _NoNote(InMemoryWindowCounter):
    """A counter whose pending note cannot be written."""

    async def note(self, text_key: str, text: str, sets: dict[str, str], ttl_seconds: int) -> None:
        raise ConnectionError("down")


async def test_a_denial_that_cannot_be_noted_for_a_summary_is_recorded_itself() -> None:
    """Fail open to completeness also when only the pending note fails."""
    sink, metric = Sink(), Metric()
    throttle = _throttle(sink, _NoNote(), Clock(), metric)
    for _ in range(7):
        await throttle.denied(_denial())
    assert len(sink.rows) == 7
    assert metric.total == 0


async def test_flush_ignores_open_future_and_malformed_windows_and_empty_bases() -> None:
    """Only closed numeric windows are summarised; the rest stay."""
    counter = InMemoryWindowCounter()
    sink, clock = Sink(), Clock()
    throttle = _throttle(sink, counter, clock)
    now = int(clock.now // WINDOW)
    await counter.sadd(PENDING_KEY, "empty", 100)
    await counter.sadd(PENDING_KEY, "open", 100)
    for window in (str(now), str(now + 5), "x"):
        await counter.sadd(f"{PENDING_KEY}:open", window, 100)
    assert await throttle.flush() == 0
    assert await counter.smembers(PENDING_KEY) == {"empty", "open"}  # bases expire, not removed
    assert await counter.smembers(f"{PENDING_KEY}:open") == {str(now), str(now + 5), "x"}
    assert sink.rows == []


async def test_an_expired_count_drops_the_window_without_a_row() -> None:
    """A pending window whose count is gone has nothing left to summarise (and is no error)."""
    counter = InMemoryWindowCounter()
    sink, clock = Sink(), Clock()
    throttle = _throttle(sink, counter, clock)
    await counter.sadd(PENDING_KEY, "abc", 100)
    await counter.sadd(f"{PENDING_KEY}:abc", "1", 100)
    assert await throttle.flush() == 0
    assert await counter.smembers(f"{PENDING_KEY}:abc") == set()


async def test_a_lost_sample_is_logged_not_invented(caplog: pytest.LogCaptureFixture) -> None:
    """Without the sample there is no subject to name: log it, drop the window."""
    counter = InMemoryWindowCounter()
    sink, clock = Sink(), Clock()
    throttle = _throttle(sink, counter, clock)
    for _ in range(7):
        await throttle.denied(_denial())
    counter.texts.clear()
    clock.now += WINDOW
    with caplog.at_level(logging.ERROR):
        assert await throttle.flush() == 0
    assert "lost its sample" in caplog.text
    assert counter.sets.get(f"{PENDING_KEY}:{throttle._base(_denial())}") is None


async def test_one_failing_entry_does_not_stop_the_flush() -> None:
    """A store error on one entry is logged; the entry stays pending and the rest go on."""
    valkey = FakeRedis()
    sink, clock = Sink(), Clock()
    throttle = _throttle(sink, ValkeyWindowCounter(as_redis(valkey)), clock)
    for sub in ("a", "b"):
        for _ in range(7):
            await throttle.denied(_denial(sub=sub))
    clock.now += WINDOW
    real = valkey.get
    calls = 0

    async def flaky(name: str) -> str | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("blip")
        return await real(name)

    valkey.get = flaky  # type: ignore[method-assign]
    assert await throttle.flush() == 1
    valkey.get = real  # type: ignore[method-assign]
    assert await throttle.flush() == 1  # the failed entry was still pending


class _FlakySink(Sink):
    """A sink that fails its first `failures` summary writes."""

    def __init__(self, failures: int) -> None:
        """Fail `failures` times."""
        super().__init__()
        self.failures = failures

    async def denied(self, denial: Denial) -> None:
        """Fail, or record."""
        if self.failures and denial.suppressed:
            self.failures -= 1
            raise ConnectionError("db down")
        await super().denied(denial)


async def test_a_failing_old_summary_never_fails_the_request_nor_loses_the_window() -> None:
    """The current denial is recorded first; the old summary is retried by the next pass."""
    sink, clock = _FlakySink(1), Clock()
    throttle = _throttle(sink, InMemoryWindowCounter(), clock)
    for _ in range(7):
        await throttle.denied(_denial())
    clock.now += WINDOW
    await throttle.denied(_denial(resource="fresh"))  # does not raise
    assert sink.rows[-1].tenant_id == "fresh"
    assert [r.suppressed for r in sink.rows if r.suppressed] == []
    assert await throttle.flush() == 1
    assert [(r.window, r.suppressed) for r in sink.rows if r.suppressed] == [(100, 2)]


async def test_a_flush_cut_short_is_continued_by_the_next_one() -> None:
    """Shutdown cancels a flush mid-write: nothing was claimed, so the next start writes it."""
    valkey = FakeRedis()
    clock = Clock()

    class Hanging(Sink):
        async def denied(self, denial: Denial) -> None:
            if denial.suppressed:
                await asyncio.sleep(10)

    throttle = _throttle(Hanging(), ValkeyWindowCounter(as_redis(valkey)), clock)
    for _ in range(7):
        await throttle.denied(_denial())
    clock.now += WINDOW
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await throttle.flush()
    sink = Sink()
    restarted = _throttle(sink, ValkeyWindowCounter(as_redis(valkey)), clock)
    assert await restarted.flush() == 1
    assert [r.suppressed for r in sink.rows] == [2]


async def test_a_denial_counted_after_the_summary_gets_a_second_summary() -> None:
    """A request that straddled the window end is not lost: the window becomes pending again."""
    counter = InMemoryWindowCounter()
    sink, clock = Sink(), Clock()
    throttle = _throttle(sink, counter, clock)
    late = _throttle(sink, counter, Clock(6000.0))  # a replica still in the old window
    for _ in range(8):
        await throttle.denied(_denial())
    clock.now += WINDOW
    assert await throttle.flush() == 1
    await late.denied(_denial(resource="late"))  # suppressed in window 100, after its summary
    assert await throttle.flush() == 1
    assert [(r.window, r.suppressed) for r in sink.rows if r.suppressed] == [(100, 3), (100, 1)]
    assert await throttle.flush() == 0


async def test_a_denial_counted_while_the_summary_is_written_is_put_back() -> None:
    """The recheck in the completion step: the window returns to the index and is summarised."""
    counter = InMemoryWindowCounter()
    clock = Clock()
    late = _throttle(Sink(), counter, Clock(6000.0))
    fired = False

    class Racing(Sink):
        async def denied(self, denial: Denial) -> None:
            nonlocal fired
            await super().denied(denial)
            if denial.suppressed and not fired:
                fired = True
                await late.denied(_denial(resource="late"))

    sink = Racing()
    throttle = _throttle(sink, counter, clock)
    for _ in range(8):
        await throttle.denied(_denial())
    clock.now += WINDOW
    assert await throttle.flush() == 1
    assert await throttle.flush() == 1
    assert [(r.window, r.suppressed) for r in sink.rows if r.suppressed] == [(100, 3), (100, 1)]


async def test_a_base_whose_index_cannot_be_read_is_skipped_and_retried() -> None:
    """The failure is logged; the other bases go on, and the next flush gets this one."""
    valkey = FakeRedis()
    sink, clock = Sink(), Clock()
    throttle = _throttle(sink, ValkeyWindowCounter(as_redis(valkey)), clock)
    for _ in range(7):
        await throttle.denied(_denial())
    clock.now += WINDOW
    real = valkey.smembers
    calls = 0

    async def flaky(name: str) -> set[str]:
        nonlocal calls
        calls += 1
        if calls == 2:  # the first call lists the bases, the second reads this base's windows
            raise ConnectionError("blip")
        return await real(name)

    valkey.smembers = flaky  # type: ignore[method-assign]
    assert await throttle.flush() == 0
    valkey.smembers = real  # type: ignore[method-assign]
    assert await throttle.flush() == 1


async def test_a_denial_writes_at_most_three_old_summaries_and_flush_does_the_rest() -> None:
    """Five pending windows: three inline writes (oldest first), two left for the flush."""
    counter = InMemoryWindowCounter()
    sink, clock = _FlakySink(1000), Clock()
    throttle = _throttle(sink, counter, clock)
    for n in range(5):  # one suppressed denial in each of five windows; summaries keep failing
        stale = _throttle(sink, counter, Clock(6000.0 + n * WINDOW))
        for _ in range(BURST + 1):
            await stale.denied(_denial())
    sink.failures = 0
    sink.rows.clear()
    clock.now += 10 * WINDOW
    await throttle.denied(_denial(resource="fresh"))
    assert [r.window for r in sink.rows if r.suppressed] == [100, 101, 102]
    assert await throttle.flush() == 2
    assert [r.window for r in sink.rows if r.suppressed] == [100, 101, 102, 103, 104]
