"""The fixed-window rate limiter shared by the webhook route and the admin API.

ADR 0028 "Abuse limits on the webhook route" (invariants 25-28) and ADR 0033 decision 5: a
counter per key in Valkey so every api replica shares it: `INCR` on `<prefix>:<key>:<window
index>` and `EXPIRE .. NX` of twice the window, in one `MULTI/EXEC` pipeline. The whole check,
connect included, is bounded by `timeout_ms`; any Valkey error or timeout fails open (the
`failopen` counter plus a rate-limited log line). The address key reads only the ASGI client
address; it never parses `X-Forwarded-For`.

A leaf module: it imports no other trustbridge module, so `webhooks` and `admin` both use it.
"""

import asyncio
import ipaddress
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from redis.asyncio import Redis

logger = logging.getLogger(__name__)
UNKNOWN_ADDRESS = "unknown"
IPV6_PREFIX = 64
_LOG_EVERY_SECONDS = 60.0


class CounterLike(Protocol):
    """The part of an OpenTelemetry counter the limiter uses."""

    def add(self, amount: int) -> None:
        """Add `amount`."""
        ...


class WindowCounter(Protocol):
    """Atomically increments a windowed counter and returns the new count."""

    async def incr(self, key: str, ttl_seconds: int) -> int:
        """`INCR key`; set the expiry to `ttl_seconds` when the key is created."""
        ...

    async def get(self, key: str) -> int:
        """The current count of `key` (0 when absent or expired)."""
        ...

    async def set_text(self, key: str, value: str, ttl_seconds: int) -> None:
        """Store `value` under `key` (replacing any value) for `ttl_seconds`."""
        ...

    async def get_text(self, key: str) -> str | None:
        """The text under `key`, or None when absent or expired."""
        ...

    async def sadd(self, key: str, member: str, ttl_seconds: int) -> None:
        """Add `member` to the set `key` and set the set's expiry to `ttl_seconds`."""
        ...

    async def smembers(self, key: str) -> set[str]:
        """The members of the set `key` (empty when absent)."""
        ...

    async def srem(self, key: str, member: str) -> None:
        """Remove `member` from the set `key` (a set left empty ceases to exist)."""
        ...

    async def delete(self, key: str) -> None:
        """Remove `key`, whatever it holds."""
        ...


class ValkeyWindowCounter:
    """`INCR` + `EXPIRE NX` in one transaction on a redis-py asyncio client."""

    def __init__(self, client: Redis) -> None:
        """Bind the client (shared with the rest of the process)."""
        self._r = client

    async def incr(self, key: str, ttl_seconds: int) -> int:
        """Atomic increment with a first-increment expiry."""
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, ttl_seconds, nx=True)
            count, _ = await pipe.execute()
        return int(count)

    async def get(self, key: str) -> int:
        """`GET key` as an integer (0 when absent)."""
        value = await self._r.get(key)
        return 0 if value is None else int(value)

    async def set_text(self, key: str, value: str, ttl_seconds: int) -> None:
        """`SET key value EX ttl`."""
        await self._r.set(key, value, ex=ttl_seconds)

    async def get_text(self, key: str) -> str | None:
        """`GET key` as text (None when absent)."""
        value = await self._r.get(key)
        if value is None:
            return None
        return value.decode() if isinstance(value, bytes) else str(value)

    async def sadd(self, key: str, member: str, ttl_seconds: int) -> None:
        """`SADD` and refresh the set's expiry in one transaction."""
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.sadd(key, member)
            pipe.expire(key, ttl_seconds)
            await pipe.execute()

    async def smembers(self, key: str) -> set[str]:
        """`SMEMBERS key` as text."""
        return {m.decode() if isinstance(m, bytes) else str(m) for m in await self._r.smembers(key)}

    async def srem(self, key: str, member: str) -> None:
        """`SREM key member`."""
        await self._r.srem(key, member)

    async def delete(self, key: str) -> None:
        """`DEL key`."""
        await self._r.delete(key)


class InMemoryWindowCounter:
    """Process-local counter for `MODE=mock` and unit tests; expired keys are evicted.

    A key lives `ttl_seconds` from its creation, as in Valkey; a sweep (at most once a second)
    drops the expired ones so rotating per-principal keys cannot grow the dict without bound.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """Start empty."""
        self.counts: dict[str, int] = {}
        self.texts: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self._expires: dict[str, float] = {}
        self._clock = clock
        self._next_sweep = 0.0

    def _live(self, key: str) -> bool:
        """Whether `key` has not expired (a key without an expiry never does)."""
        return self._expires.get(key, float("inf")) > self._clock()

    def _sweep(self) -> None:
        now = self._clock()
        if now < self._next_sweep:
            return
        self._next_sweep = now + 1.0
        for key in [k for k, at in self._expires.items() if at <= now]:
            del self._expires[key]
            self.counts.pop(key, None)
            self.texts.pop(key, None)
            self.sets.pop(key, None)

    async def incr(self, key: str, ttl_seconds: int) -> int:
        """Increment in process memory; the expiry is set when the key is created."""
        self._sweep()
        if not self._live(key):
            self.counts.pop(key, None)
            self._expires.pop(key, None)
        if key not in self.counts:
            self._expires[key] = self._clock() + ttl_seconds
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def get(self, key: str) -> int:
        """Read from process memory (0 when absent or expired)."""
        return self.counts.get(key, 0) if self._live(key) else 0

    async def set_text(self, key: str, value: str, ttl_seconds: int) -> None:
        """Store text with an expiry."""
        self._sweep()
        self.texts[key] = value
        self._expires[key] = self._clock() + ttl_seconds

    async def get_text(self, key: str) -> str | None:
        """The text, or None when absent or expired."""
        return self.texts.get(key) if self._live(key) else None

    async def sadd(self, key: str, member: str, ttl_seconds: int) -> None:
        """Add to a set and refresh its expiry."""
        self._sweep()
        if not self._live(key):
            self.sets.pop(key, None)
        self.sets.setdefault(key, set()).add(member)
        self._expires[key] = self._clock() + ttl_seconds

    async def smembers(self, key: str) -> set[str]:
        """A copy of the set (empty when absent or expired)."""
        return set(self.sets.get(key, ())) if self._live(key) else set()

    async def srem(self, key: str, member: str) -> None:
        """Remove from a set; an emptied set is dropped, as Valkey does."""
        members = self.sets.get(key)
        if members is None:
            return
        members.discard(member)
        if not members:
            del self.sets[key]
            self._expires.pop(key, None)

    async def delete(self, key: str) -> None:
        """Remove the key from every kind of store."""
        for store in (self.counts, self.texts, self.sets, self._expires):
            store.pop(key, None)


def source_key(host: str | None) -> str:
    """The limiter key for a client address: IPv6 reduced to its /64, missing as `unknown`."""
    if not host:
        return UNKNOWN_ADDRESS
    text = host.split("%", 1)[0]  # drop an IPv6 zone id
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return text[:64]  # not an IP (a test client, a Unix peer): used as given, bounded
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return (
            str(ipaddress.ip_network(f"{ip}/{IPV6_PREFIX}", strict=False).network_address)
            + f"/{IPV6_PREFIX}"
        )
    return str(ip)


@dataclass(frozen=True, slots=True)
class LimitDecision:
    """The outcome of a limiter check."""

    allowed: bool
    retry_after: int = 0


@dataclass(slots=True)
class FixedWindowLimiter:
    """A fixed-window limiter over string keys (an address, a principal)."""

    counter: WindowCounter
    enabled: bool
    requests: int
    window_seconds: int
    timeout_ms: int
    exempt: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = field(default_factory=list)
    clock: Callable[[], float] = time.time
    prefix: str = "tb:rl"
    rejected: CounterLike | None = None
    failopen: CounterLike | None = None
    _last_log: float = field(default=float("-inf"), repr=False)

    def _exempt(self, host: str | None) -> bool:
        """Whether the address is in an exempt CIDR."""
        if not host:
            return False
        try:
            ip = ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            return False
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        return any(ip.version == net.version and ip in net for net in self.exempt)

    async def check(self, host: str | None) -> LimitDecision:
        """Count this request against its source address (IPv6 as /64); exempt CIDRs pass."""
        if not self.enabled or self._exempt(host):
            return LimitDecision(True)
        return await self.check_key(source_key(host))

    async def check_key(self, key: str) -> LimitDecision:
        """Count this request against `key`; reject when it is over the limit in this window."""
        if not self.enabled:
            return LimitDecision(True)
        now = self.clock()
        window = int(now // self.window_seconds)
        full_key = f"{self.prefix}:{key}:{window}"
        try:
            async with asyncio.timeout(self.timeout_ms / 1000):
                count = await self.counter.incr(full_key, 2 * self.window_seconds)
        except Exception as exc:  # fail open on any Valkey error or on the timeout
            self._failed_open(exc)
            return LimitDecision(True)
        if count > self.requests:
            left = (window + 1) * self.window_seconds - now
            if self.rejected is not None:
                self.rejected.add(1)
            return LimitDecision(False, retry_after=max(1, math.ceil(left)))
        return LimitDecision(True)

    def _failed_open(self, exc: Exception) -> None:
        """Count the fail-open and log it at most once a minute."""
        if self.failopen is not None:
            self.failopen.add(1)
        now = time.monotonic()
        if now - self._last_log >= _LOG_EVERY_SECONDS:
            self._last_log = now
            logger.warning(
                "rate limiter failed open",
                extra={"prefix": self.prefix, "error_type": type(exc).__name__},
            )
