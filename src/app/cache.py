"""Shared server-side cache for the generic `/stats` and `/predict` routes.

Implements `craftainer_crud.interfaces.cache.StatsCache` over this app's Redis
service (`redis.asyncio`, `Settings.redis_url`, so `rediss://` TLS works exactly as it
does for `app.rate_limit`), or in process under `MODE=mock`. Only non-per-user aggregates
go through it (docs/adrs/0023-http-conditional-requests-and-stats-cache.md); the
router builds keys that already separate callers by ownership scope.

Off unless `Settings.stats_cache_ttl_seconds` is set: `build_stats_cache` then returns
None and the routes behave exactly as before.

Keys are namespaced by the app version (`APP_VERSION`, else the installed distribution's
version) so a canary release whose response shape differs never reads the other
version's entries (Phase 8.6). Invalidation ignores the version: a write by either
version drops both versions' entries for that resource.

Staleness is bounded in two ways. Writes publish a CRUD event (ADR-0015) that every
replica's `run_invalidation` task turns into `invalidate(resource)`; the TTL covers
whatever that misses (a read racing a write, a replica that was disconnected, a Redis
outage during invalidation). A Redis outage never fails a request: the cache degrades to
a miss and logs a warning.
"""

import asyncio
import contextlib
import logging
import os
import time
from importlib import metadata
from typing import cast

import redis.asyncio as aioredis
from craftainer_crud.interfaces.base import EventSource
from craftainer_crud.interfaces.cache import StatsCache
from redis.exceptions import RedisError

from app.config import Settings

logger = logging.getLogger(__name__)

_KEY_PREFIX = "stats-cache"
_SCAN_COUNT = 500
_DISTRIBUTION = "template-fastapi"


def app_version() -> str:
    """`APP_VERSION` if set, else the installed distribution's version, else "unknown"."""
    if version := os.environ.get("APP_VERSION"):
        return version
    try:
        return metadata.version(_DISTRIBUTION)
    except metadata.PackageNotFoundError:  # pragma: no cover -- always installed in tests
        return "unknown"


def _glob_escape(value: str) -> str:
    """Escape Redis glob metacharacters in `value` for a SCAN MATCH pattern."""
    return "".join(f"\\{char}" if char in "*?[]\\" else char for char in value)


class RedisStatsCache:
    """`StatsCache` over `redis.asyncio`: entries expire after `ttl_seconds`."""

    def __init__(self, client: aioredis.Redis, *, ttl_seconds: int, version: str) -> None:
        """Bind the Redis client, the entry TTL and the app version that namespaces keys."""
        self._client = client
        self._ttl_seconds = ttl_seconds
        self._version = version

    @classmethod
    def from_url(cls, url: str, *, ttl_seconds: int, version: str) -> RedisStatsCache:
        """Build a cache over a lazily-connecting client for `url` (`redis://` or `rediss://`)."""
        return cls(aioredis.Redis.from_url(url), ttl_seconds=ttl_seconds, version=version)

    def _redis_key(self, resource: str, key: str) -> str:
        return f"{_KEY_PREFIX}:{self._version}:{resource}:{key}"

    async def get(self, resource: str, key: str) -> bytes | None:
        """Return the cached body, or None on a miss or a Redis failure."""
        try:
            value = await self._client.get(self._redis_key(resource, key))
        except RedisError:  # pragma: no cover -- Redis outage; unit-tested, not reachable in e2e
            logger.warning("stats cache get failed", exc_info=True)
            return None
        # The client is built without decode_responses, so a hit is always bytes.
        return cast("bytes | None", value)

    async def set(self, resource: str, key: str, value: bytes) -> None:
        """Store `value` with the TTL; a Redis failure is logged and otherwise ignored."""
        try:
            await self._client.set(self._redis_key(resource, key), value, ex=self._ttl_seconds)
        except RedisError:  # pragma: no cover -- Redis outage; unit-tested, not reachable in e2e
            logger.warning("stats cache set failed", exc_info=True)

    async def invalidate(self, resource: str) -> None:
        """Delete every entry of `resource`, across app versions; failures fall back to the TTL."""
        pattern = f"{_KEY_PREFIX}:*:{_glob_escape(resource)}:*"
        try:
            batch: list[bytes] = []
            async for redis_key in self._client.scan_iter(match=pattern, count=_SCAN_COUNT):
                batch.append(redis_key)
                # e2e-unreachable: needs 500+ cached keys; covered by tests/unit/test_cache.py
                if len(batch) >= _SCAN_COUNT:  # pragma: no cover
                    await self._client.unlink(*batch)
                    batch = []
            if batch:
                await self._client.unlink(*batch)
        except RedisError:  # pragma: no cover -- Redis outage; unit-tested, not reachable in e2e
            logger.warning("stats cache invalidate failed", exc_info=True)

    async def aclose(self) -> None:
        """Close the Redis client and its connection pool (call from the lifespan teardown)."""
        await self._client.aclose()


class InMemoryStatsCache:
    """In-process `StatsCache` for MODE=mock (no Redis): same TTL semantics, per process."""

    def __init__(self, *, ttl_seconds: int, version: str) -> None:
        """Set the entry TTL and the app version namespacing keys, as `RedisStatsCache` does.

        Entries live in a dict of resource -> versioned key -> (expiry, body).
        """
        self._ttl_seconds = ttl_seconds
        self._version = version
        self._entries: dict[str, dict[str, tuple[float, bytes]]] = {}

    async def get(self, resource: str, key: str) -> bytes | None:
        """Return the cached body, or None on a miss or an expired entry."""
        entry = self._entries.get(resource, {}).get(f"{self._version}:{key}")
        if entry is None or entry[0] <= time.monotonic():
            return None
        return entry[1]

    async def set(self, resource: str, key: str, value: bytes) -> None:
        """Store `value` until the TTL elapses."""
        self._entries.setdefault(resource, {})[f"{self._version}:{key}"] = (
            time.monotonic() + self._ttl_seconds,
            value,
        )

    async def invalidate(self, resource: str) -> None:
        """Drop every entry of `resource`."""
        self._entries.pop(resource, None)


def build_stats_cache(settings: Settings) -> StatsCache | None:
    """Return the cache `settings` asks for, or None when `STATS_CACHE_TTL_SECONDS` is unset."""
    ttl = settings.stats_cache_ttl_seconds
    if ttl is None:
        return None
    if settings.mode == "mock":
        return InMemoryStatsCache(ttl_seconds=ttl, version=app_version())
    return RedisStatsCache.from_url(settings.redis_url, ttl_seconds=ttl, version=app_version())


async def run_invalidation(
    cache: StatsCache,
    source: EventSource,
    *,
    resource: str,
    retry_seconds: float = 5.0,
) -> None:
    """Invalidate `resource`'s cache entries on every CRUD event from `source`, until cancelled.

    Meant to run for the life of a replica as a lifespan background task, one per cached
    resource (`asyncio.create_task(run_invalidation(...))`, cancelled on shutdown); every
    replica runs its own, so a write handled by any replica reaches every replica's cache
    through the event stream (ADR-0015). Each subscribe is transient
    (`source.subscribe(None, durable=False)`: a fresh id and a clean MQTT session the broker
    drops when the connection ends, cleanly or not), so a replica leaves no session behind
    and nothing is resumed.

    On every (re)subscribe the resource is invalidated once, covering events that could
    not have been delivered while disconnected (a transient session queues none). A failing
    subscription or stream is logged and retried after `retry_seconds`; cancellation
    propagates.
    """
    while True:
        try:
            _, events = await source.subscribe(None, durable=False)
            # aclosing: the iterator owns the subscription (craftainer-crud ADR 0006), so
            # it must be closed on every exit, including a failing invalidate() or a cancel.
            async with contextlib.aclosing(events):
                await cache.invalidate(resource)
                async for _event in events:
                    await cache.invalidate(resource)
        # e2e-unreachable: needs a failing event stream; covered by tests/unit/test_cache.py
        except Exception:  # pragma: no cover
            logger.warning("stats cache invalidation stream failed", exc_info=True)
        await asyncio.sleep(retry_seconds)
