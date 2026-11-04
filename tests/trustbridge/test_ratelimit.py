"""The shared fixed-window limiter (ADR 0033 decision 5): keys, prefixes, counter reads."""

from tests.trustbridge.fakes import FakeRedis
from tests.trustbridge.helpers import as_redis
from trustbridge.ratelimit import FixedWindowLimiter, InMemoryWindowCounter, ValkeyWindowCounter


def _limiter(counter: object, **kw: object) -> FixedWindowLimiter:
    values: dict[str, object] = {
        "counter": counter,
        "enabled": True,
        "requests": 1,
        "window_seconds": 60,
        "timeout_ms": 50,
        "clock": lambda: 600.0,
        "prefix": "tb:rl:test",
    }
    values.update(kw)
    return FixedWindowLimiter(**values)  # type: ignore[arg-type]


async def test_keys_are_counted_under_the_prefix_and_separately() -> None:
    """One key's count does not move another's; the stored key carries the prefix and window."""
    counter = InMemoryWindowCounter()
    limiter = _limiter(counter)
    assert (await limiter.check_key("a")).allowed
    assert not (await limiter.check_key("a")).allowed
    assert (await limiter.check_key("b")).allowed
    assert set(counter.counts) == {"tb:rl:test:a:10", "tb:rl:test:b:10"}


async def test_a_disabled_limiter_counts_nothing() -> None:
    """`enabled=False` neither counts nor rejects."""
    counter = InMemoryWindowCounter()
    limiter = _limiter(counter, enabled=False)
    assert (await limiter.check_key("a")).allowed
    assert (await limiter.check("203.0.113.1")).allowed
    assert counter.counts == {}


async def test_counters_read_back_what_they_incremented() -> None:
    """`get` is 0 for an absent key on both counters."""
    memory = InMemoryWindowCounter()
    valkey = ValkeyWindowCounter(as_redis(FakeRedis()))
    for counter in (memory, valkey):
        assert await counter.get("k") == 0
        await counter.incr("k", 10)
        await counter.incr("k", 10)
        assert await counter.get("k") == 2


async def test_failures_without_metrics_still_fail_open() -> None:
    """No counters are required."""
    valkey = FakeRedis()
    valkey.down = True
    limiter = _limiter(ValkeyWindowCounter(as_redis(valkey)))
    assert (await limiter.check_key("a")).allowed
