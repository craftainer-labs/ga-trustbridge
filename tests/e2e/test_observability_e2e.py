"""E2E journeys for the deployment-wiring features: request IDs, OTel export, /health/info,
on-demand profiling, the shared stats cache and rate limiting.

These depend on the environment `conftest.py` gives the api process it starts (an OTLP
receiver, the stats cache, `HEALTH_INFO_ENABLED`, `PROFILING_ENABLED`, ...), so each one
skips when something else already serves the base URL.
"""

import json
import time
import uuid
from collections.abc import Callable

import httpx
import pytest
from playwright.sync_api import Page

from tests.e2e.conftest import AppHandle

_HEROES = "/crud/v1/heroes/v2/json"
_WAIT_SECONDS = 10.0


@pytest.fixture
def managed(app_handle: AppHandle) -> AppHandle:
    """The api handle, skipping the test when the api's environment isn't ours."""
    if not app_handle.managed:
        pytest.skip("the api was started elsewhere; its environment is unknown")
    return app_handle


def _wait_for(condition: Callable[[], bool], what: str) -> None:
    """Poll `condition` until true; fail with `what` after the wait budget."""
    deadline = time.monotonic() + _WAIT_SECONDS
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.1)
    pytest.fail(f"timed out waiting for {what}")


def _log_lines(handle: AppHandle) -> list[dict[str, object]]:
    """The api process' JSON log lines so far (plain-text lines, e.g. uvicorn's, are skipped)."""
    assert handle.log_path is not None
    lines: list[dict[str, object]] = []
    for raw in handle.log_path.read_text().splitlines():
        try:
            parsed = json.loads(raw)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            lines.append(parsed)
    return lines


def test_request_id_is_echoed_and_an_invalid_one_is_replaced(page: Page, base_url: str) -> None:
    """A valid X-Request-ID comes back unchanged; a malformed one is replaced by a fresh ID."""
    echoed = page.request.get(f"{base_url}/health/live", headers={"X-Request-ID": "e2e-req-1"})
    assert echoed.headers["x-request-id"] == "e2e-req-1"

    replaced = page.request.get(f"{base_url}/health/live", headers={"X-Request-ID": "bad id!"})
    assert replaced.headers["x-request-id"] != "bad id!"
    assert len(replaced.headers["x-request-id"]) == 32

    generated = page.request.get(f"{base_url}/health/live")
    assert len(generated.headers["x-request-id"]) == 32


def test_request_id_and_trace_id_reach_the_json_logs(managed: AppHandle, base_url: str) -> None:
    """A rejected token logs a warning carrying the request's ID and the trace ID.

    Runs on every leg, including the ones that migrate on startup: alembic/env.py leaves an
    already-configured root logger alone, so the JSON handler survives the migrations.
    """
    request_id = f"e2e-log-{uuid.uuid4().hex}"
    response = httpx.get(
        f"{base_url}{_HEROES}",
        headers={"Authorization": "Bearer not-a-token", "X-Request-ID": request_id},
        timeout=10,
    )
    assert response.status_code == 401

    def logged() -> bool:
        return any(line.get("request_id") == request_id for line in _log_lines(managed))

    _wait_for(logged, "the request's log line")
    line = next(line for line in _log_lines(managed) if line.get("request_id") == request_id)
    if managed.leg == "bare":  # the bare leg runs without OpenTelemetry: no spans, no IDs
        assert "trace_id" not in line
        return
    assert len(str(line["trace_id"])) == 32
    assert len(str(line["span_id"])) == 16


def test_access_log_follows_uvicorn_access_log_setting(managed: AppHandle, base_url: str) -> None:
    """uvicorn's access lines are JSON with the request ID, or absent when switched off.

    The mock leg runs with UVICORN_ACCESS_LOG=false; the others keep uvicorn's default.
    """
    request_id = f"e2e-access-{uuid.uuid4().hex}"
    marker_id = f"e2e-access-marker-{uuid.uuid4().hex}"
    assert httpx.get(
        f"{base_url}/health/live", headers={"X-Request-ID": request_id}, timeout=10
    ).is_success
    # A rejected token logs an app warning: once it's in the log, everything the server
    # wrote for the earlier request is too, so absence can be asserted without sleeping.
    httpx.get(
        f"{base_url}{_HEROES}",
        headers={"Authorization": "Bearer not-a-token", "X-Request-ID": marker_id},
        timeout=10,
    )
    _wait_for(
        lambda: any(line.get("request_id") == marker_id for line in _log_lines(managed)),
        "the marker request's log line",
    )
    access = [
        line
        for line in _log_lines(managed)
        if line.get("logger") == "uvicorn.access" and line.get("request_id") == request_id
    ]
    if managed.leg == "mock":
        assert access == []
        return
    _wait_for(
        lambda: any(
            line.get("logger") == "uvicorn.access" and line.get("request_id") == request_id
            for line in _log_lines(managed)
        ),
        "the request's uvicorn.access line",
    )


def test_spans_and_metrics_arrive_at_the_otlp_receiver(
    managed: AppHandle, base_url: str, access_token: Callable[[str], str]
) -> None:
    """A traced request exports a span (tagged with its request ID) and request metrics."""
    if managed.leg != "dev":
        pytest.skip("only the dev leg exports traces and metrics")
    assert managed.otlp is not None
    otlp = managed.otlp
    request_id = f"e2e-span-{uuid.uuid4().hex}"
    response = httpx.get(
        f"{base_url}{_HEROES}",
        headers={
            "Authorization": f"Bearer {access_token('viewer')}",
            "X-Request-ID": request_id,
        },
        timeout=10,
    )
    assert response.status_code == 200

    _wait_for(
        lambda: any(request_id.encode() in body for body in otlp.bodies("/v1/traces")),
        "a span carrying the request ID",
    )
    _wait_for(
        lambda: any(b"http.server" in body for body in otlp.bodies("/v1/metrics")),
        "HTTP server metrics",
    )
    # Health probes are excluded from tracing (DEFAULT_EXCLUDED_URLS).
    assert not any(b"/health/live" in body for body in otlp.bodies("/v1/traces"))


def test_health_info_reports_version_and_commit_when_enabled(
    managed: AppHandle, page: Page, base_url: str
) -> None:
    """GET /health/info returns the configured version (or the installed one) and commit.

    Off by default, so the bare leg answers 404 instead.
    """
    response = page.request.get(f"{base_url}/health/info")
    if managed.leg == "bare":
        assert response.status == 404
        return
    assert response.ok
    assert response.json() == {
        "version": managed.expected_version,
        "commit": managed.expected_commit,
    }


def test_profile_query_returns_an_html_profile_when_enabled(
    managed: AppHandle, page: Page, base_url: str
) -> None:
    """`?profile=1` answers with pyinstrument's HTML report; with profiling off it is ignored."""
    response = page.request.get(f"{base_url}/health/live?profile=1")
    assert response.ok
    if managed.leg == "bare":
        assert response.json() == {"status": "ok"}
        return
    assert response.headers["content-type"].startswith("text/html")
    assert "pyinstrument" in response.text().lower()
    assert page.request.get(f"{base_url}/health/live").json() == {"status": "ok"}


def test_stats_cache_misses_then_hits_then_misses_after_a_write(
    managed: AppHandle, base_url: str, access_token: Callable[[str], str]
) -> None:
    """/stats is cached: miss then hit, and a hero write empties it through the event stream.

    The bare leg runs without the cache, so its responses carry no cache header.
    """
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    with httpx.Client(base_url=base_url, headers=headers, timeout=10) as http:
        if managed.leg == "bare":
            response = http.get(f"{_HEROES}/stats")
            assert response.status_code == 200
            assert "x-stats-cache" not in response.headers
            return

        def stats_state() -> str:
            response = http.get(f"{_HEROES}/stats")
            assert response.status_code == 200
            return response.headers["x-stats-cache"]

        def settled_miss_then_hit() -> bool:
            # Another journey's write may still be invalidating; retry until a read is
            # stored and the very next one is served from the cache.
            return stats_state() == "miss" and stats_state() == "hit"

        _wait_for(settled_miss_then_hit, "a /stats miss followed by a hit")

        created = http.post(_HEROES, json={"name": "E2E Cache Hero", "powers": ["Speed"]})
        assert created.status_code == 201
        try:
            _wait_for(lambda: stats_state() == "miss", "invalidation after a hero write")
        finally:
            http.delete(_HEROES, params={"id": created.json()["id"]})


def test_rate_limit_answers_429_past_the_budget(managed: AppHandle, base_url: str) -> None:
    """POST /mock/token past its per-client budget answers 429 problem details.

    The mock leg's api honors X-Forwarded-For (conftest sets FORWARDED_ALLOW_IPS), so this
    journey burns a client key of its own and leaves everyone else's budget alone. The dev
    api has no token route and its bulk routes share one key, so only this leg runs it.
    """
    if managed.leg != "mock":
        pytest.skip("needs POST /mock/token and a trusted X-Forwarded-For (mock leg only)")
    client_key = {"X-Forwarded-For": f"203.0.113.{uuid.uuid4().int % 250 + 1}"}
    statuses: list[int] = []
    with httpx.Client(base_url=base_url, headers=client_key, timeout=10) as http:
        # RATE_LIMIT_MOCK_TOKEN is 1000/minute in this suite; the 1001st request trips it.
        for _ in range(1001):
            response = http.post("/mock/token", json={"sub": "limit", "roles": []})
            statuses.append(response.status_code)
            if response.status_code == 429:
                break
    assert statuses[-1] == 429
    assert set(statuses[:-1]) == {200}
    assert response.headers["content-type"].startswith("application/problem+json")
