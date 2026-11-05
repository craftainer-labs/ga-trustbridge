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


class _Clock:
    """A settable clock."""

    def __init__(self) -> None:
        """Start at 0."""
        self.now = 0.0

    def __call__(self) -> float:
        """The time."""
        return self.now


async def test_the_in_memory_counter_evicts_expired_keys() -> None:
    """Keys live `ttl` from their creation; a sweep frees the dict (no unbounded growth)."""
    clock = _Clock()
    counter = InMemoryWindowCounter(clock)
    for n in range(100):
        await counter.incr(f"principal:{n}", 10)
    await counter.set_text("t", "v", 10)
    await counter.sadd("s", "m", 10)
    assert len(counter.counts) == 100
    assert await counter.incr("principal:0", 10) == 2  # not expired: same window
    clock.now = 11.0
    assert await counter.get("principal:0") == 0
    assert await counter.get_text("t") is None
    assert await counter.smembers("s") == set()
    assert await counter.incr("fresh", 10) == 1  # the sweep runs on write
    assert set(counter.counts) == {"fresh"}
    assert (counter.texts, counter.sets) == ({}, {})


async def test_an_expired_key_restarts_from_one_even_before_the_sweep() -> None:
    """Reading an expired key never returns the old count, whenever the sweep runs."""
    clock = _Clock()
    counter = InMemoryWindowCounter(clock)
    await counter.incr("k", 10)
    await counter.incr("k", 10)
    await counter.sadd("s", "old", 10)
    clock.now = 10.5
    counter._next_sweep = 100.0
    assert await counter.incr("k", 10) == 1
    await counter.sadd("s", "new", 10)
    assert await counter.smembers("s") == {"new"}


async def test_text_and_sets_round_trip_on_both_counters() -> None:
    """The pending-set and sample primitives behave the same in memory and in Valkey."""
    for counter in (InMemoryWindowCounter(), ValkeyWindowCounter(as_redis(FakeRedis()))):
        assert await counter.get_text("t") is None
        await counter.set_text("t", "one", 10)
        await counter.set_text("t", "two", 10)
        assert await counter.get_text("t") == "two"
        assert await counter.smembers("s") == set()
        await counter.sadd("s", "a", 10)
        await counter.sadd("s", "a", 10)
        await counter.sadd("s", "b", 10)
        assert await counter.smembers("s") == {"a", "b"}
        await counter.srem("s", "a")
        assert await counter.smembers("s") == {"b"}
        await counter.srem("s", "b")
        await counter.srem("absent", "x")
        assert await counter.smembers("s") == set()
        await counter.delete("t")
        await counter.delete("t")
        assert await counter.get_text("t") is None
