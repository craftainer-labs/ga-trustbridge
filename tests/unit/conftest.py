"""Shared unit-test fixtures: an all-roles auth override, and analytics isolation."""

from collections.abc import Callable, Iterator

import pytest
from craftainer_observability.analytics import shutdown

from app.config import get_settings
from app.main import app
from app.oidc import get_current_claims

ALL_ROLES = ["viewer", "editor", "maintainer", "security", "detective"]


_ANALYTICS_ENV = (
    "ANALYTICS_ENABLED",
    "ANALYTICS_BACKEND",
    "ANALYTICS_ENDPOINT",
    "ANALYTICS_SITE_ID",
    "ANALYTICS_HOSTNAME",
    "ANALYTICS_CONSENT",
    "ANALYTICS_RESPECT_GPC",
    "ANALYTICS_GEO",
    "ANALYTICS_PROP_KEYS",
)


@pytest.fixture(autouse=True)
def analytics_off(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No test reaches a real analytics backend, whatever the devcontainer's env says.

    Clears every ANALYTICS_* variable (the stack's umami.env sets the backend inside the
    devcontainer) and drops the library's process-wide client, which reads them once.
    """
    for name in _ANALYTICS_ENV:
        monkeypatch.delenv(name, raising=False)
    shutdown()
    yield
    shutdown()


@pytest.fixture
def analytics_log(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Return a function that switches `track()` to the in-process `log` backend.

    Keyword arguments become ANALYTICS_* variables (`consent="opt-in"` sets
    ANALYTICS_CONSENT); the library's client is rebuilt from them. The events then appear as
    log records on `craftainer_observability.analytics.events`.
    """

    def _configure(**settings: str) -> None:
        monkeypatch.setenv("ANALYTICS_BACKEND", "log")
        for key, value in settings.items():
            monkeypatch.setenv(f"ANALYTICS_{key.upper()}", value)
        shutdown()

    return _configure


@pytest.fixture
def authed() -> Iterator[None]:
    """Override get_current_claims to grant every RBAC role, for the test's duration."""
    settings = get_settings()
    app.dependency_overrides[get_current_claims] = lambda: {
        "sub": "test-user",
        "resource_access": {settings.oidc_client_id: {"roles": ALL_ROLES}},
    }
    yield
    del app.dependency_overrides[get_current_claims]
