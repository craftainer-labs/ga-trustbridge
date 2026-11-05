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
    summary, fresh = sink.rows[5:]
    assert (summary.suppressed, summary.window_seconds, summary.sub) == (4, WINDOW, "mallory")
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
    assert sink.rows[5].suppressed == 2


async def test_a_gap_of_several_windows_still_summarises_the_old_window() -> None:
    """Path (a) claims every closed pending window, not only the previous one."""
    sink, clock = Sink(), Clock()
    throttle = _throttle(sink, InMemoryWindowCounter(), clock)
    for _ in range(9):
        await throttle.denied(_denial())
    clock.now += 3 * WINDOW
    await throttle.denied(_denial(resource="fresh"))
    summary, fresh = sink.rows[5:]
    assert (summary.suppressed, fresh.tenant_id, fresh.suppressed) == (4, "fresh", 0)


async def test_every_closed_window_of_the_pair_gets_its_own_summary() -> None:
    """Two suppressed windows, one later denial: two rows, oldest first, each counted once."""
    sink, clock = Sink(), Clock()
    throttle = _throttle(sink, InMemoryWindowCounter(), clock)
    for _ in range(8):  # 3 suppressed
        await throttle.denied(_denial())
    clock.now += WINDOW
    for _ in range(10):  # 5 suppressed
        await throttle.denied(_denial())
    clock.now += 4 * WINDOW
    await throttle.denied(_denial(resource="later"))
    assert [r.suppressed for r in sink.rows if r.suppressed] == [3, 5]
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


async def test_two_replicas_flushing_at_once_write_one_row() -> None:
    """The claim marker: racing flushes, and a flush racing a later denial, give one row."""
    valkey = FakeRedis()
    sink, clock = Sink(), Clock()
    one = _throttle(sink, ValkeyWindowCounter(as_redis(valkey)), clock)
    two = _throttle(sink, ValkeyWindowCounter(as_redis(valkey)), clock)
    for _ in range(9):
        await one.denied(_denial())
    clock.now += WINDOW
    await asyncio.gather(one.flush(), two.flush(), two.denied(_denial()))
    assert [r.suppressed for r in sink.rows].count(4) == 1


def _claim_key(sub: str = "mallory", *, window: int) -> str:
    return ThrottledDenials._claim_key(
        _throttle(Sink(), InMemoryWindowCounter(), Clock()),
        ThrottledDenials._base(_denial(sub=sub)),
        window,
    )


async def test_a_window_claimed_elsewhere_is_not_written_again() -> None:
    """Another replica holds the claim marker: this one drops the pending entry, writes nothing."""
    counter = InMemoryWindowCounter()
    sink, clock = Sink(), Clock()
    throttle = _throttle(sink, counter, clock)
    for _ in range(7):
        await throttle.denied(_denial())
    window = int(clock.now // WINDOW)
    await counter.incr(_claim_key(window=window), 100)
    clock.now += WINDOW
    assert await throttle.flush() == 0
    assert (counter.sets, len(sink.rows)) == ({}, 5)


async def test_a_later_denial_skips_a_window_another_replica_already_claimed() -> None:
    """Path (a) with the claim marker taken elsewhere: no summary, the new denial is recorded."""
    counter = InMemoryWindowCounter()
    sink, clock = Sink(), Clock()
    throttle = _throttle(sink, counter, clock)
    for _ in range(7):
        await throttle.denied(_denial())
    window = int(clock.now // WINDOW)
    await counter.incr(_claim_key(window=window), 100)
    clock.now += WINDOW
    await throttle.denied(_denial(resource="fresh"))
    assert [r.suppressed for r in sink.rows] == [0] * 6
    assert counter.sets == {}


async def test_a_denial_straddling_the_window_end_is_recorded_not_lost() -> None:
    """The window was summarised already: a late suppressed denial is a row, not a lost count."""
    counter = InMemoryWindowCounter()
    sink, metric = Sink(), Metric()
    throttle = _throttle(sink, counter, Clock(), metric)
    for _ in range(5):
        await throttle.denied(_denial())
    await counter.incr(_claim_key(window=int(6000 // WINDOW)), 100)
    await throttle.denied(_denial(resource="late"))
    assert (len(sink.rows), sink.rows[-1].tenant_id, metric.total) == (6, "late", 0)
    assert counter.sets == {}


async def test_the_summary_describes_the_last_suppressed_denial() -> None:
    """The row's sample is the last suppressed denial, with its address and request id."""
    sink, clock = Sink(), Clock()
    throttle = _throttle(sink, InMemoryWindowCounter(), clock)
    for n in range(7):
        await throttle.denied(replace(_denial(resource=f"t{n}"), source_ip=f"192.0.2.{n}"))
    clock.now += WINDOW
    await throttle.flush()
    assert (sink.rows[-1].tenant_id, sink.rows[-1].source_ip) == ("t6", "192.0.2.6")


class _NoSets(InMemoryWindowCounter):
    """A counter whose pending index cannot be written."""

    async def sadd(self, key: str, member: str, ttl_seconds: int) -> None:
        raise ConnectionError("down")


async def test_a_denial_that_cannot_be_noted_for_a_summary_is_recorded_itself() -> None:
    """Fail open to completeness also when only the pending note fails."""
    sink, metric = Sink(), Metric()
    throttle = _throttle(sink, _NoSets(), Clock(), metric)
    for _ in range(7):
        await throttle.denied(_denial())
    assert len(sink.rows) == 7
    assert metric.total == 0


async def test_flush_forgets_empty_bases_and_ignores_future_and_malformed_windows() -> None:
    """An index entry without windows is dropped; open and malformed windows stay."""
    counter = InMemoryWindowCounter()
    sink, clock = Sink(), Clock()
    throttle = _throttle(sink, counter, clock)
    now = int(clock.now // WINDOW)
    await counter.sadd(PENDING_KEY, "empty", 100)
    await counter.sadd(PENDING_KEY, "open", 100)
    for window in (str(now), str(now + 5), "x"):
        await counter.sadd(f"{PENDING_KEY}:open", window, 100)
    assert await throttle.flush() == 0
    assert await counter.smembers(PENDING_KEY) == {"open"}
    assert await counter.smembers(f"{PENDING_KEY}:open") == {str(now), str(now + 5), "x"}
    assert sink.rows == []


async def test_an_expired_count_drops_the_pending_entry_without_a_row() -> None:
    """A pending window whose count is gone has nothing left to summarise (and is no error)."""
    counter = InMemoryWindowCounter()
    sink, clock = Sink(), Clock()
    throttle = _throttle(sink, counter, clock)
    await counter.sadd(PENDING_KEY, "abc", 100)
    await counter.sadd(f"{PENDING_KEY}:abc", "1", 100)
    assert await throttle.flush() == 0
    assert counter.sets == {}


async def test_a_lost_sample_is_logged_not_invented(caplog: pytest.LogCaptureFixture) -> None:
    """Without the sample there is no subject to name: log it, drop the entry."""
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
    assert counter.sets == {}


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
    """A sink that fails its first `failures` writes."""

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


async def test_a_summary_whose_row_could_not_be_written_is_retried() -> None:
    """The claim is released when the sink fails, so the window is not lost."""
    valkey = FakeRedis()
    sink, clock = _FlakySink(1), Clock()
    throttle = _throttle(sink, ValkeyWindowCounter(as_redis(valkey)), clock)
    for _ in range(7):
        await throttle.denied(_denial())
    clock.now += WINDOW
    with pytest.raises(ConnectionError):
        await throttle.flush()
    assert await throttle.flush() == 1
    assert [r.suppressed for r in sink.rows if r.suppressed] == [2]


async def test_a_summary_written_but_still_indexed_is_not_written_twice() -> None:
    """Forgetting the entry failed after the row: the next pass finds the claim and forgets it."""
    valkey = FakeRedis()
    sink, clock = Sink(), Clock()
    throttle = _throttle(sink, ValkeyWindowCounter(as_redis(valkey)), clock)
    for _ in range(7):
        await throttle.denied(_denial())
    clock.now += WINDOW
    real = valkey.srem

    async def failing(name: str, *values: str) -> int:
        raise ConnectionError("blip")

    valkey.srem = failing  # type: ignore[method-assign]
    assert await throttle.flush() == 1
    valkey.srem = real  # type: ignore[method-assign]
    assert await throttle.flush() == 0
    assert valkey.sets == {}
    assert [r.suppressed for r in sink.rows if r.suppressed] == [2]


class _FlakyGet(InMemoryWindowCounter):
    """A counter whose `get` fails on its n-th call once armed."""

    def __init__(self) -> None:
        """Start unarmed."""
        super().__init__()
        self.fail_on: int | None = None

    async def get(self, key: str) -> int:
        """Count calls; fail on the armed one."""
        if self.fail_on is not None:
            self.fail_on -= 1
            if self.fail_on == 0:
                raise ConnectionError("blip")
        return await super().get(key)


async def test_summaries_claimed_before_a_failure_are_still_written() -> None:
    """Two closed windows; the second read fails: the first, already claimed, is written."""
    counter = _FlakyGet()
    sink, clock = Sink(), Clock()
    throttle = _throttle(sink, counter, clock)
    for _ in range(8):
        await throttle.denied(_denial())
    clock.now += WINDOW
    counter.fail_on = 1  # the claim of the first window fails, so it stays pending
    for _ in range(7):
        await throttle.denied(_denial())
    assert [r.suppressed for r in sink.rows if r.suppressed] == []
    clock.now += 3 * WINDOW
    counter.fail_on = 2
    await throttle.denied(_denial(resource="fresh"))
    assert [r.suppressed for r in sink.rows if r.suppressed] == [3]
    assert sink.rows[-1].tenant_id == "fresh"
    counter.fail_on = None
    assert await throttle.flush() == 1  # the window whose read failed is still pending
    assert [r.suppressed for r in sink.rows if r.suppressed] == [3, 2]
