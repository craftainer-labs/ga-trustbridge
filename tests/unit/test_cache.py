"""Unit test: app.cache's Redis and in-memory StatsCache, app_version, and run_invalidation."""

import asyncio
import re
import time
from collections.abc import AsyncGenerator, AsyncIterator
from importlib import metadata
from typing import Any

import pytest
from craftainer_crud.interfaces.base import EventIterator, InMemoryEventSink
from redis.exceptions import ConnectionError as RedisConnectionError

from app import cache as cache_module
from app.cache import (
    InMemoryStatsCache,
    RedisStatsCache,
    build_stats_cache,
    run_invalidation,
)
from app.config import Settings


def _glob_to_regex(pattern: str) -> str:
    """Translate the subset of Redis glob syntax app.cache emits (`*`, `?`, `\\x`)."""
    out: list[str] = []
    chars = iter(pattern)
    for char in chars:
        if char == "\\":
            out.append(re.escape(next(chars)))
        elif char == "*":
            out.append(".*")
        elif char == "?":
            out.append(".")
        else:
            out.append(re.escape(char))
    return "".join(out)


class _FakeRedis:
    """The slice of redis.asyncio.Redis that RedisStatsCache uses, backed by a dict."""

    def __init__(self, *, fail: bool = False) -> None:
        self.store: dict[str, bytes] = {}
        self.ttls: dict[str, int | None] = {}
        self.fail = fail
        self.closed = False
        self.unlink_batches: list[int] = []

    def _check(self) -> None:
        if self.fail:
            raise RedisConnectionError("down")

    async def get(self, name: str) -> bytes | None:
        self._check()
        return self.store.get(name)

    async def set(self, name: str, value: bytes, ex: int | None = None) -> None:
        self._check()
        self.store[name] = value
        self.ttls[name] = ex

    async def scan_iter(self, match: str, count: int) -> AsyncIterator[str]:
        self._check()
        regex = re.compile(_glob_to_regex(match))
        for name in list(self.store):
            if regex.fullmatch(name):
                yield name

    async def unlink(self, *names: str) -> int:
        self.unlink_batches.append(len(names))
        for name in names:
            del self.store[name]
        return len(names)

    async def aclose(self) -> None:
        self.closed = True


def _cache(client: _FakeRedis, version: str = "1.0") -> RedisStatsCache:
    return RedisStatsCache(client, ttl_seconds=30, version=version)  # type: ignore[arg-type]


async def test_redis_cache_round_trip_with_ttl_and_version_namespace() -> None:
    """set stores under the versioned key with the TTL; get returns it; a miss is None."""
    client = _FakeRedis()
    cache = _cache(client)
    assert await cache.get("hero", "k") is None
    await cache.set("hero", "k", b"body")
    assert client.store == {"stats-cache:1.0:hero:k": b"body"}
    assert client.ttls == {"stats-cache:1.0:hero:k": 30}
    assert await cache.get("hero", "k") == b"body"
    assert await _cache(client, "2.0").get("hero", "k") is None


async def test_redis_cache_invalidate_drops_one_resource_across_versions() -> None:
    """invalidate removes every version's entries of the resource and leaves others alone."""
    client = _FakeRedis()
    await _cache(client, "1.0").set("hero", "a", b"1")
    await _cache(client, "2.0").set("hero", "b", b"2")
    await _cache(client, "1.0").set("hero", "c", b"3")
    await _cache(client, "1.0").set("hero*", "d", b"4")
    await _cache(client, "1.0").set("villain", "a", b"5")
    await _cache(client).invalidate("hero")
    assert sorted(client.store) == ["stats-cache:1.0:hero*:d", "stats-cache:1.0:villain:a"]


async def test_redis_cache_invalidate_escapes_glob_characters() -> None:
    """A resource name containing glob metacharacters only matches itself."""
    client = _FakeRedis()
    await _cache(client).set("a*", "k", b"1")
    await _cache(client).set("ab", "k", b"2")
    await _cache(client).invalidate("a*")
    assert list(client.store) == ["stats-cache:1.0:ab:k"]


async def test_redis_cache_invalidate_unlinks_in_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    """More matches than the scan batch size are unlinked in several calls."""
    monkeypatch.setattr(cache_module, "_SCAN_COUNT", 2)
    client = _FakeRedis()
    cache = _cache(client)
    for index in range(5):
        await cache.set("hero", str(index), b"x")
    await cache.invalidate("hero")
    assert client.store == {}
    assert client.unlink_batches == [2, 2, 1]
    for index in range(4):  # an exact multiple of the batch size leaves no trailing batch
        await cache.set("hero", str(index), b"x")
    client.unlink_batches.clear()
    await cache.invalidate("hero")
    assert client.unlink_batches == [2, 2]


async def test_redis_cache_degrades_to_a_miss_when_redis_is_down(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """get/set/invalidate swallow RedisError (logged), so a Redis outage never fails a request."""
    cache = _cache(_FakeRedis(fail=True))
    assert await cache.get("hero", "k") is None
    await cache.set("hero", "k", b"x")
    await cache.invalidate("hero")
    assert caplog.text.count("stats cache") == 3


async def test_redis_cache_aclose_closes_the_client() -> None:
    """aclose releases the Redis client."""
    client = _FakeRedis()
    await _cache(client).aclose()
    assert client.closed


def test_redis_cache_from_url_builds_a_lazy_client() -> None:
    """from_url accepts redis:// and rediss:// URLs without connecting."""
    for url in ("redis://localhost:6379/0", "rediss://localhost:6380/0"):
        cache = RedisStatsCache.from_url(url, ttl_seconds=5, version="v")
        assert cache._ttl_seconds == 5


async def test_in_memory_cache_expires_and_invalidates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Entries are served until the TTL, per resource, and invalidate drops a resource."""
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    cache = InMemoryStatsCache(ttl_seconds=10, version="v")
    assert await cache.get("hero", "k") is None
    await cache.set("hero", "k", b"1")
    await cache.set("villain", "k", b"2")
    assert await cache.get("hero", "k") == b"1"
    now[0] = 111.0
    assert await cache.get("hero", "k") is None
    await cache.set("hero", "k", b"3")
    await cache.invalidate("hero")
    assert await cache.get("hero", "k") is None
    assert await cache.get("villain", "k") is None  # expired at 110
    await cache.invalidate("hero")  # idempotent


async def test_in_memory_cache_namespaces_keys_by_version() -> None:
    """Entries are stored under the app version, like the Redis keys."""
    cache = InMemoryStatsCache(ttl_seconds=10, version="1.2.3")
    await cache.set("hero", "k", b"1")
    assert list(cache._entries["hero"]) == ["1.2.3:k"]


def test_build_stats_cache_versions_the_in_memory_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mock-mode cache gets app_version() as its key namespace."""
    monkeypatch.setenv("APP_VERSION", "7.7.7")
    mock = build_stats_cache(Settings(mode="mock", allow_mock_mode=True, stats_cache_ttl_seconds=5))
    assert isinstance(mock, InMemoryStatsCache)
    assert mock._version == "7.7.7"


def test_build_stats_cache_by_settings() -> None:
    """Off without a TTL; in-memory under MODE=mock; Redis otherwise."""
    assert build_stats_cache(Settings()) is None
    mock = build_stats_cache(Settings(mode="mock", allow_mock_mode=True, stats_cache_ttl_seconds=5))
    assert isinstance(mock, InMemoryStatsCache)
    real = build_stats_cache(Settings(stats_cache_ttl_seconds=5))
    assert isinstance(real, RedisStatsCache)


def test_app_version_prefers_env_then_distribution(monkeypatch: pytest.MonkeyPatch) -> None:
    """APP_VERSION wins; otherwise the installed distribution's version is used."""
    monkeypatch.setenv("APP_VERSION", "9.9.9")
    assert cache_module.app_version() == "9.9.9"
    monkeypatch.delenv("APP_VERSION")
    assert cache_module.app_version() == metadata.version("template-fastapi")


class _RecordingCache:
    def __init__(self) -> None:
        self.invalidated: list[str] = []

    async def get(self, resource: str, key: str) -> bytes | None:
        return None

    async def set(self, resource: str, key: str, value: bytes) -> None:
        return None

    async def invalidate(self, resource: str) -> None:
        self.invalidated.append(resource)


class _Stream:
    """EventIterator that counts aclose() calls, even on a stream that was never started."""

    def __init__(self, inner: AsyncGenerator[dict[str, Any]], source: _ScriptedSource) -> None:
        self._inner = inner
        self._source = source

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> dict[str, Any]:
        return await self._inner.__anext__()

    async def aclose(self) -> None:
        self._source.closed += 1
        await self._inner.aclose()


class _ScriptedSource:
    """EventSource: each subscribe() consumes the next script (events, or an exception)."""

    def __init__(self, scripts: list[list[dict[str, Any]] | Exception]) -> None:
        self.scripts = scripts
        self.subscribed_with: list[tuple[str | None, bool]] = []
        self.closed = 0

    async def subscribe(
        self, subscriber_id: str | None, *, durable: bool = True
    ) -> tuple[str, EventIterator[dict[str, Any]]]:
        self.subscribed_with.append((subscriber_id, durable))
        script = self.scripts.pop(0)
        if isinstance(script, Exception):
            raise script
        return "sub", _Stream(self._iterate(script), self)

    async def _iterate(self, events: list[dict[str, Any]]) -> AsyncGenerator[dict[str, Any]]:
        for event in events:
            yield event
        if not self.scripts:
            await asyncio.Event().wait()  # last stream stays open until cancelled


async def test_run_invalidation_accepts_the_real_in_memory_event_source() -> None:
    """The installed craftainer_crud source takes `durable=False`; a published event invalidates."""
    cache = _RecordingCache()
    sink = InMemoryEventSink("hero")
    task = asyncio.create_task(run_invalidation(cache, sink, resource="hero"))
    for _ in range(50):
        await asyncio.sleep(0)
        if cache.invalidated:
            break
    await sink.publish(resource="hero", record_id=1, action="create", snapshot={})
    for _ in range(50):
        await asyncio.sleep(0)
        if len(cache.invalidated) >= 2:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cache.invalidated == ["hero", "hero"]


async def test_run_invalidation_invalidates_on_subscribe_and_each_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Resubscribes after failures, invalidating on each subscribe and each event."""
    cache = _RecordingCache()
    source = _ScriptedSource([RuntimeError("broker down"), [{"a": 1}, {"a": 2}], [{"a": 3}]])
    task = asyncio.create_task(run_invalidation(cache, source, resource="hero", retry_seconds=0))
    for _ in range(50):
        await asyncio.sleep(0)
        if len(cache.invalidated) >= 6:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # failed subscribe: nothing; stream 1: subscribe + 2 events; stream 2: subscribe + 1 event
    assert cache.invalidated == ["hero"] * 5
    # every subscribe is transient with a fresh id; the id the source returned is not carried over
    assert source.subscribed_with == [(None, False)] * 3
    assert "invalidation stream failed" in caplog.text


class _FailingCache(_RecordingCache):
    async def invalidate(self, resource: str) -> None:
        raise RuntimeError("redis down")


async def test_run_invalidation_closes_the_event_iterator_when_invalidate_fails() -> None:
    """The iterator owns the subscription (craftainer-crud ADR 0006): it is closed on a failure."""
    source = _ScriptedSource([[{"a": 1}], [{"a": 2}]])
    task = asyncio.create_task(
        run_invalidation(_FailingCache(), source, resource="hero", retry_seconds=0)
    )
    for _ in range(50):
        await asyncio.sleep(0)
        if source.closed >= 2:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert source.closed >= 2
