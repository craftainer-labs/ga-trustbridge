"""Unit test: app.analytics's GPC/DNT and consent signals reach the library's track()."""

import logging
from collections.abc import Callable
from typing import Any

import pytest
from craftainer_observability.analytics import get_request_context, track
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from app.analytics import AnalyticsContextMiddleware

_EVENTS_LOGGER = "craftainer_observability.analytics.events"


def _build_app() -> FastAPI:
    """A tiny app whose routes each record one event, behind the middleware under test."""
    app = FastAPI()
    app.add_middleware(AnalyticsContextMiddleware)

    @app.get("/sync")
    def sync_route() -> dict[str, bool]:
        track("demo.sync_hit")
        return {"ok": True}

    @app.get("/async")
    async def async_route() -> dict[str, bool]:
        track("demo.async_hit")
        return {"ok": True}

    return app


def _tracked(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Names of the events the `log` backend recorded so far."""
    return [r.__dict__["analytics_event"] for r in caplog.records if r.name == _EVENTS_LOGGER]


@pytest.fixture
def client(caplog: pytest.LogCaptureFixture) -> TestClient:
    """A client for the tiny app, with the log backend's records captured."""
    caplog.set_level(logging.INFO, logger=_EVENTS_LOGGER)
    return TestClient(_build_app())


def test_events_are_tracked_when_the_request_carries_no_signal(
    client: TestClient, analytics_log: Callable[..., None], caplog: pytest.LogCaptureFixture
) -> None:
    """Implied consent and no GPC/DNT: both a sync and an async route record their event."""
    analytics_log()
    assert client.get("/sync").status_code == 200
    assert client.get("/async").status_code == 200
    assert _tracked(caplog) == ["demo.sync_hit", "demo.async_hit"]


@pytest.mark.parametrize("headers", [{"Sec-GPC": "1"}, {"DNT": "1"}, {"Sec-GPC": "1", "DNT": "0"}])
def test_gpc_or_dnt_turns_tracking_off_for_that_request(
    client: TestClient,
    analytics_log: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
    headers: dict[str, str],
) -> None:
    """`Sec-GPC: 1` or `DNT: 1` drops the request's events; the next request is unaffected."""
    analytics_log()
    assert client.get("/sync", headers=headers).status_code == 200
    assert client.get("/async", headers=headers).status_code == 200
    assert _tracked(caplog) == []
    assert client.get("/sync").status_code == 200
    assert _tracked(caplog) == ["demo.sync_hit"]


@pytest.mark.parametrize(
    "headers", [{"Sec-GPC": "0"}, {"DNT": "0"}, {"DNT": "yes"}, {"Sec-GPC": ""}]
)
def test_only_the_value_1_counts_as_a_signal(
    client: TestClient,
    analytics_log: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
    headers: dict[str, str],
) -> None:
    """`Sec-GPC: 0`, `DNT: 0` and any other value do not turn tracking off."""
    analytics_log()
    assert client.get("/sync", headers=headers).status_code == 200
    assert _tracked(caplog) == ["demo.sync_hit"]


def test_gpc_is_ignored_when_respect_gpc_is_off(
    client: TestClient, analytics_log: Callable[..., None], caplog: pytest.LogCaptureFixture
) -> None:
    """ANALYTICS_RESPECT_GPC=false is the operator's explicit opt-out of honouring the signal."""
    analytics_log(respect_gpc="false")
    assert client.get("/sync", headers={"Sec-GPC": "1", "DNT": "1"}).status_code == 200
    assert _tracked(caplog) == ["demo.sync_hit"]


def test_opt_in_records_nothing_without_the_consent_header(
    client: TestClient, analytics_log: Callable[..., None], caplog: pytest.LogCaptureFixture
) -> None:
    """ANALYTICS_CONSENT=opt-in: no Analytics-Consent: 1, no event."""
    analytics_log(consent="opt-in")
    assert client.get("/sync").status_code == 200
    assert client.get("/sync", headers={"Analytics-Consent": "0"}).status_code == 200
    assert _tracked(caplog) == []


def test_opt_in_records_with_the_consent_header(
    client: TestClient, analytics_log: Callable[..., None], caplog: pytest.LogCaptureFixture
) -> None:
    """ANALYTICS_CONSENT=opt-in: `Analytics-Consent: 1` lets the request's events through."""
    analytics_log(consent="opt-in")
    assert client.get("/sync", headers={"Analytics-Consent": "1"}).status_code == 200
    assert _tracked(caplog) == ["demo.sync_hit"]


def test_gpc_beats_consent(
    client: TestClient, analytics_log: Callable[..., None], caplog: pytest.LogCaptureFixture
) -> None:
    """A consent header does not override Global Privacy Control."""
    analytics_log(consent="opt-in")
    response = client.get("/sync", headers={"Analytics-Consent": "1", "Sec-GPC": "1"})
    assert response.status_code == 200
    assert _tracked(caplog) == []


def test_consent_header_changes_nothing_under_implied_consent(
    client: TestClient, analytics_log: Callable[..., None], caplog: pytest.LogCaptureFixture
) -> None:
    """With ANALYTICS_CONSENT=implied the consent header is irrelevant."""
    analytics_log()
    assert client.get("/sync", headers={"Analytics-Consent": "1"}).status_code == 200
    assert _tracked(caplog) == ["demo.sync_hit"]


def test_nothing_is_tracked_when_analytics_is_disabled_or_has_no_backend(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The default backend is `none`: the routes answer normally and record nothing."""
    assert client.get("/sync").status_code == 200
    assert _tracked(caplog) == []


def test_context_does_not_outlive_the_request() -> None:
    """The request context is reset once the request has been served."""
    seen: list[object] = []
    app = FastAPI()
    app.add_middleware(AnalyticsContextMiddleware)

    @app.get("/ctx")
    def ctx_route() -> dict[str, bool]:
        context = get_request_context()
        assert context is not None
        seen.append((context.gpc, context.consent))
        return {"ok": True}

    response = TestClient(app).get("/ctx", headers={"DNT": "1", "Analytics-Consent": "1"})
    assert response.status_code == 200
    assert seen == [(True, True)]
    assert get_request_context() is None


async def test_non_http_scopes_pass_through_without_a_context() -> None:
    """A lifespan (or websocket) scope is delegated untouched, with no request context."""
    seen: dict[str, Any] = {}

    async def _inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen["scope"] = scope["type"]
        seen["context"] = get_request_context()

    async def _receive() -> Message:
        return {}

    async def _send(_message: Message) -> None:
        return None

    await AnalyticsContextMiddleware(_inner)({"type": "lifespan"}, _receive, _send)
    assert seen == {"scope": "lifespan", "context": None}
