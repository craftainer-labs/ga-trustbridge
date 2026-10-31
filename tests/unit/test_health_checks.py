"""Unit test: get_health_registry's wiring from Settings, and the mounted /health routes.

The checks themselves and the generic registry/router framework are tested in the
craftainer-observability package repo.
"""

import pytest
from craftainer_observability.health.base import HealthCheckResult
from craftainer_observability.health.registry import HealthRegistry
from fastapi.testclient import TestClient

from app import health_checks
from app.config import get_settings
from app.health_checks import MockHealthCheck, close_health_checks, get_health_registry
from app.main import app

client = TestClient(app)


async def test_mock_health_check_always_reports_healthy() -> None:
    """MockHealthCheck reports healthy without touching the network."""
    result = await MockHealthCheck("database").check()
    assert result.name == "database"
    assert result.healthy is True
    assert result.detail == "mocked"


def test_get_health_registry_registers_mock_checks_in_mock_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_health_registry() registers four MockHealthChecks when MODE=mock."""
    get_health_registry.cache_clear()
    monkeypatch.setattr(get_settings(), "mode", "mock")
    try:
        registry = get_health_registry()
        assert all(isinstance(check, MockHealthCheck) for check, _ in registry._checks)
        assert {check.name for check, _ in registry._checks} == {
            "database",
            "redis",
            "s3",
            "oidc",
        }
    finally:
        get_health_registry.cache_clear()


def test_get_health_registry_registers_real_checks_outside_mock_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_health_registry() wires the four real checks under their service names otherwise."""
    get_health_registry.cache_clear()
    monkeypatch.setattr(get_settings(), "mode", "real")
    try:
        registry = get_health_registry()
        assert not any(isinstance(check, MockHealthCheck) for check, _ in registry._checks)
        assert {check.name for check, _ in registry._checks} == {
            "database",
            "redis",
            "s3",
            "oidc",
        }
    finally:
        get_health_registry.cache_clear()


class _FakeCheck:
    """A HealthCheck stand-in with a fixed outcome."""

    name = "fake"

    def __init__(self, *, healthy: bool) -> None:
        """Remember the fixed result this fake will report."""
        self._healthy = healthy

    async def check(self) -> HealthCheckResult:
        """Return the fixed result this fake was constructed with."""
        return HealthCheckResult(self.name, healthy=self._healthy)


def _fake_registry(*, healthy: bool) -> HealthRegistry:
    """Build a registry with one fake check, healthy or not as requested."""
    registry = HealthRegistry()
    registry.register(_FakeCheck(healthy=healthy))
    return registry


def test_health_live_route_is_mounted() -> None:
    """GET /health/live returns 200 and the static ok payload, no dependency checks."""
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_ready_route_reflects_the_wired_registry() -> None:
    """GET /health/ready runs whatever registry get_health_registry currently wires in."""
    app.dependency_overrides[get_health_registry] = lambda: _fake_registry(healthy=True)
    try:
        response = client.get("/health/ready")
    finally:
        del app.dependency_overrides[get_health_registry]
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["fake"]["healthy"] is True


def test_health_ready_route_reports_degraded_on_failure() -> None:
    """GET /health/ready returns 503 when the wired registry has an unhealthy check."""
    app.dependency_overrides[get_health_registry] = lambda: _fake_registry(healthy=False)
    try:
        response = client.get("/health/ready")
    finally:
        del app.dependency_overrides[get_health_registry]
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


async def test_close_health_checks_closes_the_redis_checks_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown closes the RedisCheck's reused client once it has been created."""
    closed: list[str] = []

    class _Redis:
        async def aclose(self) -> None:
            closed.append("redis")

    health_checks._redis_check.cache_clear()
    try:
        await close_health_checks()  # nothing created yet: a no-op
        assert closed == []
        monkeypatch.setattr(health_checks._redis_check(), "_client", _Redis())
        await close_health_checks()
        assert closed == ["redis"]
    finally:
        health_checks._redis_check.cache_clear()
