"""The webhook abuse limiter (ADR 0028 "Abuse limits on the webhook route"; invariants 25-28).

A fixed-window counter per source address, kept in Valkey so every api replica shares it:
`INCR` on `tb:rl:webhooks:<address>:<window index>` and `EXPIRE .. NX` of twice the window,
in one `MULTI/EXEC` pipeline. It is the first step of the webhook route, before the
`Content-Length` check, any body read and the signature check, so bad-signature requests
count. It reads only `request.client.host` (the ASGI client address, which uvicorn's
`ProxyHeadersMiddleware` rewrites for `TRUSTED_PROXIES`) and never parses `X-Forwarded-For`.
The whole check, connect included, is bounded by `WEBHOOK_RATE_LIMIT_TIMEOUT_MS`; any Valkey
error or timeout fails open (metric plus a rate-limited log line).
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

from trustbridge.worker import metrics

logger = logging.getLogger(__name__)
KEY_PREFIX = "tb:rl:webhooks"
UNKNOWN_ADDRESS = "unknown"
IPV6_PREFIX = 64
_LOG_EVERY_SECONDS = 60.0


class WindowCounter(Protocol):
    """Atomically increments a windowed counter and returns the new count."""

    async def incr(self, key: str, ttl_seconds: int) -> int:
        """`INCR key`; set the expiry to `ttl_seconds` when the key is created."""
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


class InMemoryWindowCounter:
    """Process-local counter for `MODE=mock` and unit tests (no expiry beyond the window key)."""

    def __init__(self) -> None:
        """Start empty."""
        self.counts: dict[str, int] = {}

    async def incr(self, key: str, ttl_seconds: int) -> int:
        """Increment in process memory."""
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]


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
        return str(ipaddress.ip_network(f"{ip}/{IPV6_PREFIX}", strict=False).network_address) + f"/{IPV6_PREFIX}"
    return str(ip)


@dataclass(frozen=True, slots=True)
class LimitDecision:
    """The outcome of a limiter check."""

    allowed: bool
    retry_after: int = 0


@dataclass(slots=True)
class WebhookLimiter:
    """The per-address fixed-window limiter."""

    counter: WindowCounter
    enabled: bool
    requests: int
    window_seconds: int
    timeout_ms: int
    exempt: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = field(default_factory=list)
    clock: Callable[[], float] = time.time
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
        """Count this request; reject when the address is over the limit in this window."""
        if not self.enabled or self._exempt(host):
            return LimitDecision(True)
        now = self.clock()
        window = int(now // self.window_seconds)
        key = f"{KEY_PREFIX}:{source_key(host)}:{window}"
        try:
            async with asyncio.timeout(self.timeout_ms / 1000):
                count = await self.counter.incr(key, 2 * self.window_seconds)
        except Exception as exc:  # fail open on any Valkey error or on the timeout
            self._failed_open(exc)
            return LimitDecision(True)
        if count > self.requests:
            left = (window + 1) * self.window_seconds - now
            metrics.limiter_rejected.add(1)
            return LimitDecision(False, retry_after=max(1, math.ceil(left)))
        return LimitDecision(True)

    def _failed_open(self, exc: Exception) -> None:
        """Count the fail-open and log it at most once a minute."""
        metrics.limiter_failopen.add(1)
        now = time.monotonic()
        if now - self._last_log >= _LOG_EVERY_SECONDS:
            self._last_log = now
            logger.warning(
                "webhook limiter failed open", extra={"error_type": type(exc).__name__}
            )
