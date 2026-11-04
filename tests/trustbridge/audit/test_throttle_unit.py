"""Bounding authz.denied rows (NG6d decision A6): burst, summary on window close, restarts."""

from dataclasses import replace

import pytest

from tests.trustbridge.fakes import FakeRedis
from tests.trustbridge.helpers import as_redis
from trustbridge.audit import throttle as throttle_module
from trustbridge.audit.denial import Denial
from trustbridge.audit.throttle import ThrottledDenials
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


async def test_a_restart_keeps_the_count_and_the_next_denial_summarises() -> None:
    """Counts live in Valkey: a fresh process still summarises via path (a)."""
    valkey = FakeRedis()
    sink, clock = Sink(), Clock()
    before = _throttle(sink, ValkeyWindowCounter(as_redis(valkey)), clock)
    for _ in range(7):
        await before.denied(_denial())
    clock.now += WINDOW
    after = _throttle(sink, ValkeyWindowCounter(as_redis(valkey)), clock)  # restarted process
    assert await after.flush() == 0  # nothing indexed: this part is what a restart loses
    await after.denied(_denial())
    assert sink.rows[5].suppressed == 2


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


async def test_the_second_replica_to_notice_the_closed_window_writes_no_second_summary() -> None:
    """Both replicas see the first denial of the new window; only one claims the summary."""
    valkey = FakeRedis()
    sink, clock = Sink(), Clock()
    one = _throttle(sink, ValkeyWindowCounter(as_redis(valkey)), clock)
    two = _throttle(sink, ValkeyWindowCounter(as_redis(valkey)), clock)
    for _ in range(7):
        await one.denied(_denial())
    clock.now += WINDOW
    await one.denied(_denial())  # claims the summary
    await two.denied(_denial())  # second denial of the window: n == 2, nothing to claim
    other = _throttle(sink, ValkeyWindowCounter(as_redis(valkey)), clock)
    assert (
        await other._claim_summary(
            other._base(_denial()),
            int(clock.now // WINDOW) - 1,
            _denial(),
        )
        is None
    )
    assert [r.suppressed for r in sink.rows].count(2) == 1


async def test_the_index_of_open_windows_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the cap the count is still kept in the store, only the flush index stops growing."""
    monkeypatch.setattr(throttle_module, "MAX_OPEN_WINDOWS", 1)
    sink, clock = Sink(), Clock()
    throttle = _throttle(sink, InMemoryWindowCounter(), clock)
    for sub in ("a", "b"):
        for _ in range(6):
            await throttle.denied(_denial(sub=sub))
    clock.now += WINDOW
    assert await throttle.flush() == 1  # only "a" was indexed
