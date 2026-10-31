"""Unit test: app.main's mode-dependent wiring (debugger, mock router, migrations)."""

import asyncio
import gc
from typing import Any

import debugpy
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.main as main_module
from app.cache import InMemoryStatsCache, RedisStatsCache
from app.crud_1.heroes import heroes_v2


def test_configure_debugger_skips_non_dev_modes() -> None:
    """_configure_debugger does nothing for "production" (no debugpy call)."""
    main_module._configure_debugger("production")  # must not raise


def test_configure_debugger_enables_debugpy_for_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    """_configure_debugger("dev") calls debugpy.listen, suppressing RuntimeError."""
    calls: list[tuple[str, int]] = []

    def _fake_listen(address: tuple[str, int]) -> None:
        calls.append(address)
        raise RuntimeError("already listening")

    monkeypatch.setattr(debugpy, "listen", _fake_listen)
    main_module._configure_debugger("dev")  # must not raise despite the RuntimeError
    assert calls == [("127.0.0.1", 5678)]


def test_mount_mode_specific_routers_skips_non_mock_modes() -> None:
    """_mount_mode_specific_routers leaves a "dev"-mode app without /mock/token."""
    app = FastAPI()
    main_module._mount_mode_specific_routers(app, "dev")
    response = TestClient(app).post("/mock/token", json={"sub": "u"})
    assert response.status_code == 404


def test_mount_mode_specific_routers_mounts_mock_router() -> None:
    """_mount_mode_specific_routers("mock") adds a working POST /mock/token."""
    app = FastAPI()
    main_module._mount_mode_specific_routers(app, "mock")
    response = TestClient(app).post("/mock/token", json={"sub": "u"})
    assert response.status_code == 200


class _FakeEngine:
    """Stands in for app.db.engine: records dispose() calls."""

    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def dispose(self) -> None:
        self._events.append("dispose")


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the lifespan's side effects with recorders; return the shared event list."""
    recorded: list[str] = []

    async def _prefetch() -> None:
        recorded.append("prefetch")

    monkeypatch.setattr(main_module, "prefetch_jwks", _prefetch)
    monkeypatch.setattr(main_module, "engine", _FakeEngine(recorded))

    async def _close_health() -> None:
        recorded.append("close_health")

    monkeypatch.setattr(main_module, "close_health_checks", _close_health)
    monkeypatch.setattr(main_module, "_run_migrations", lambda: recorded.append("migrate"))

    def _collect() -> int:
        recorded.append("collect")
        return 0

    monkeypatch.setattr(gc, "collect", _collect)
    monkeypatch.setattr(gc, "freeze", lambda: recorded.append("freeze"))
    monkeypatch.setattr(main_module, "stats_cache", None)
    return recorded


async def test_lifespan_skips_migrations_when_mode_is_mock(
    monkeypatch: pytest.MonkeyPatch, events: list[str]
) -> None:
    """The lifespan context manager skips _run_migrations entirely for MODE=mock."""
    monkeypatch.setattr(main_module.settings, "mode", "mock")
    async with main_module.lifespan(main_module.app):
        pass
    assert "migrate" not in events


async def test_lifespan_skips_migrations_when_disabled(
    monkeypatch: pytest.MonkeyPatch, events: list[str]
) -> None:
    """RUN_MIGRATIONS_ON_STARTUP=false skips migrations outside mock mode too."""
    monkeypatch.setattr(main_module.settings, "mode", "dev")
    monkeypatch.setattr(main_module.settings, "run_migrations_on_startup", False)
    async with main_module.lifespan(main_module.app):
        pass
    assert "migrate" not in events


async def test_lifespan_orders_startup_and_shutdown(
    monkeypatch: pytest.MonkeyPatch, events: list[str]
) -> None:
    """Migrate, prefetch the JWKS, then collect+freeze; dispose the engine last."""
    monkeypatch.setattr(main_module.settings, "mode", "dev")
    monkeypatch.setattr(main_module.settings, "run_migrations_on_startup", True)
    monkeypatch.setattr(main_module.settings, "gc_freeze", True)
    async with main_module.lifespan(main_module.app):
        events.append("serving")
    assert events == [
        "migrate",
        "prefetch",
        "collect",
        "freeze",
        "serving",
        "close_health",
        "dispose",
    ]


async def test_lifespan_skips_gc_freeze_when_disabled(
    monkeypatch: pytest.MonkeyPatch, events: list[str]
) -> None:
    """GC_FREEZE=false leaves the garbage collector alone."""
    monkeypatch.setattr(main_module.settings, "gc_freeze", False)
    async with main_module.lifespan(main_module.app):
        pass
    assert "freeze" not in events
    assert "collect" not in events


async def test_lifespan_disposes_engine_when_startup_fails(
    monkeypatch: pytest.MonkeyPatch, events: list[str]
) -> None:
    """The engine is still disposed when the body of the lifespan raises."""
    monkeypatch.setattr(main_module.settings, "gc_freeze", False)
    with pytest.raises(RuntimeError, match="boom"):
        async with main_module.lifespan(main_module.app):
            raise RuntimeError("boom")
    assert events[-1] == "dispose"


async def test_lifespan_runs_and_cancels_stats_cache_invalidation(
    monkeypatch: pytest.MonkeyPatch, events: list[str]
) -> None:
    """With a cache, a hero-event invalidation task runs while serving and is cancelled after."""
    cache = InMemoryStatsCache(ttl_seconds=30, version="v")
    source = object()
    seen: dict[str, Any] = {}
    started = asyncio.Event()

    async def _fake_run_invalidation(
        got_cache: object, got_source: object, *, resource: str
    ) -> None:
        seen.update(cache=got_cache, source=got_source, resource=resource)
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("invalidation cancelled")
            raise

    monkeypatch.setattr(main_module, "stats_cache", cache)
    monkeypatch.setattr(main_module, "run_invalidation", _fake_run_invalidation)
    monkeypatch.setattr(heroes_v2, "get_hero_event_source", lambda: source)
    async with main_module.lifespan(main_module.app):
        await started.wait()
    assert seen == {
        "cache": cache,
        "source": source,
        "resource": "hero",
    }
    assert events.index("invalidation cancelled") < events.index("dispose")


async def test_lifespan_closes_redis_cache_after_cancelling_invalidation(
    monkeypatch: pytest.MonkeyPatch, events: list[str]
) -> None:
    """A Redis-backed cache is closed after the task is cancelled and before the engine."""
    cache = RedisStatsCache.from_url("redis://localhost:6379/0", ttl_seconds=30, version="t")

    async def _aclose() -> None:
        events.append("aclose")

    async def _fake_run_invalidation(*_args: object, **_kwargs: object) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("cancelled")
            raise

    monkeypatch.setattr(cache, "aclose", _aclose)
    monkeypatch.setattr(main_module, "stats_cache", cache)
    monkeypatch.setattr(main_module, "run_invalidation", _fake_run_invalidation)
    monkeypatch.setattr(heroes_v2, "get_hero_event_source", object)
    async with main_module.lifespan(main_module.app):
        await asyncio.sleep(0)
    assert events[-4:] == ["cancelled", "aclose", "close_health", "dispose"]


def test_docs_path_is_off_only_in_production() -> None:
    """The docs/schema routes keep their path in dev and mock, and are None in production."""
    assert main_module._docs_path("/docs", "dev") == "/docs"
    assert main_module._docs_path("/docs", "mock") == "/docs"
    assert main_module._docs_path("/docs", "production") is None


def test_app_serves_docs_and_health_info_flag_defaults() -> None:
    """In the (dev) unit environment the docs are on and /health/info is off by default."""
    client = TestClient(main_module.app)
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/health/info").status_code == 404
