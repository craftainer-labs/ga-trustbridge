"""E2E fixture overrides: point Playwright at the running myapp container,
and at a browser served by the selenium container instead of a local one.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import metadata
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import Browser, Playwright
from selenium.webdriver import ChromeOptions, Remote
from sqlalchemy import text

from app.config import get_settings
from app.db import engine

_STARTUP_TIMEOUT_SECONDS = 30.0

# Each parametrized leg gets its own port so every leg's api process can run
# for the same pytest session without colliding.
_LEG_PORTS = {"dev": 8000, "mock": 8001, "bare": 8002}

_REALM_EXPORT_PATH = (
    Path(__file__).resolve().parents[2] / ".devcontainer/stack/keycloak/realm-export.json"
)

# The api subprocess below runs the same app code the coverage gate measures
# for tests/unit and tests/integration; give it the same coverage config and
# a sitecustomize.py (via PYTHONPATH) so it starts measuring itself on launch
# and pytest-cov can combine its data with this process's at session end --
# see sitecustomize.py and pyproject.toml's [tool.coverage.run].
_COVERAGE_SUBPROCESS_ENV = {
    "COVERAGE_PROCESS_START": str(Path(__file__).resolve().parents[2] / "pyproject.toml"),
    "PYTHONPATH": os.pathsep.join(
        [
            str(Path(__file__).resolve().parent),
            *([os.environ["PYTHONPATH"]] if "PYTHONPATH" in os.environ else []),
        ]
    ),
}


# The version/commit the mock leg's api process reports on GET /health/info (APP_VERSION /
# APP_COMMIT). The dev leg leaves APP_VERSION unset, so it reports the installed
# distribution's version (and namespaces its Redis cache keys with it) -- together the
# two legs cover both lookup paths.
E2E_APP_VERSION = "e2e-1.2.3"
E2E_APP_COMMIT = "e2e-commit"
E2E_PUBLIC_BASE_URL = "https://api.e2e.example"
E2E_SECURITY_CONTACT = "mailto:security@e2e.example"
# Computed at import: Settings refuses an Expires more than a year ahead or in the past.
E2E_SECURITY_EXPIRES = (datetime.now(UTC) + timedelta(days=180)).strftime("%Y-%m-%dT%H:%M:%SZ")
_APP_VERSION_BY_LEG = {"mock": E2E_APP_VERSION}
_INSTALLED_VERSION = metadata.version("template-fastapi")

# The three legs differ in which optional features their api process runs with, so that
# every feature's enabled AND disabled branches execute in some leg (coverage is combined
# across legs):
#
# - dev:  MODE=dev against the real services. Local connection pool with every size knob,
#         TLS negotiation, asyncpg timeouts, Redis stats cache, /health/info, profiling,
#         GC freeze, and traces + metrics exported to the OTLP receiver.
# - mock: MODE=mock. In-process stats cache, /health/info with APP_VERSION, profiling, no
#         GC freeze, a local pool with the default sizes (the engine is built but never
#         connects); OTel configured but with the trace and metric exporters off.
# - bare: MODE=dev with none of the optional settings: how the app behaves by default
#         (NullPool, no cache, no /health/info, no profiling, no telemetry). Real services,
#         so journeys that need direct database access run here too.
_LEG_ENV: dict[str, dict[str, str]] = {
    "dev": {
        "DB_POOL_MODE": "local",
        "DB_POOL_SIZE": "5",
        "DB_MAX_OVERFLOW": "5",
        "DB_POOL_RECYCLE": "1800",
        "DB_CONNECT_TIMEOUT": "10",
        "DB_COMMAND_TIMEOUT": "30",
        "POSTGRES_SSLMODE": "prefer",
        "STATS_CACHE_TTL_SECONDS": "300",
        "HEALTH_INFO_ENABLED": "true",
        "PROFILING_ENABLED": "true",
        "GC_FREEZE": "true",
        "APP_COMMIT": E2E_APP_COMMIT,
        # RFC 9728 resource origin, and the optional security.txt (RFC 9116) fully
        # configured; the other legs leave both unset (defaults, security.txt off).
        "PUBLIC_BASE_URL": E2E_PUBLIC_BASE_URL,
        "SECURITY_TXT_CONTACT": E2E_SECURITY_CONTACT,
        "SECURITY_TXT_EXPIRES": E2E_SECURITY_EXPIRES,
        "SECURITY_TXT_POLICY": "https://example.com/security-policy",
        "SECURITY_TXT_PREFERRED_LANGUAGES": "en, de",
        "SECURITY_TXT_CANONICAL": "https://example.com/.well-known/security.txt",
    },
    "mock": {
        "STATS_CACHE_TTL_SECONDS": "300",
        "HEALTH_INFO_ENABLED": "true",
        "PROFILING_ENABLED": "true",
        "GC_FREEZE": "false",
        "DB_POOL_MODE": "local",
        "APP_VERSION": E2E_APP_VERSION,
        "APP_COMMIT": E2E_APP_COMMIT,
        "OTEL_TRACES_EXPORTER": "none",
        "OTEL_METRICS_EXPORTER": "none",
        # Trusted so the rate-limit journey can give itself its own client key.
        "FORWARDED_ALLOW_IPS": "*",
        # uvicorn reads UVICORN_* natively: this leg runs with the access log off, so
        # logs.configure_logging() must leave uvicorn.access silent (other legs log it).
        "UVICORN_ACCESS_LOG": "false",
    },
    "bare": {},
}
# Legs that export telemetry to the OTLP receiver (the others never set the endpoint).
_OTLP_LEGS = {"dev", "mock"}
# Settings a leg must not inherit from the surrounding environment.
_LEG_ENV_KEYS = {key for env in _LEG_ENV.values() for key in env} | {
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "DB_POOL_MODE",
    "APP_VERSION",
}


class OtlpReceiver:
    """Minimal in-process OTLP/HTTP receiver: records every POST, answers 200.

    A test stub standing in for a collector (not a new service): the api process under
    test exports traces, metrics and logs to it, and journeys assert on what arrived.
    """

    def __init__(self) -> None:
        """Bind a free loopback port and start serving in a daemon thread."""
        self.requests: list[tuple[str, bytes]] = []
        receiver = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                receiver.requests.append((self.path, self.rfile.read(length)))
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return None

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.endpoint = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def bodies(self, path: str) -> list[bytes]:
        """Every body received at `path` (e.g. "/v1/traces") so far."""
        return [body for got, body in list(self.requests) if got == path]

    def close(self) -> None:
        """Stop serving."""
        self._server.shutdown()
        self._server.server_close()


@dataclass
class AppHandle:
    """What the `_running_app` fixture knows about the api process for this leg."""

    # False when something else already served the base URL (a launch config, or
    # E2E_BASE_URL): its environment is unknown, so env-dependent journeys skip.
    managed: bool = False
    leg: str = ""
    otlp: OtlpReceiver | None = None
    log_path: Path | None = None
    expected_version: str = _INSTALLED_VERSION
    expected_commit: str = E2E_APP_COMMIT


@pytest.fixture(scope="session", params=["dev", "mock", "bare"])
def app_leg(request: pytest.FixtureRequest) -> str:
    """Which leg (`dev`, `mock` or `bare`, see `_LEG_ENV`) this parametrized run is."""
    return request.param  # type: ignore[no-any-return]


@pytest.fixture(scope="session")
def app_mode(app_leg: str) -> str:
    """Which MODE the app-under-test boots under for this leg: `dev` or `mock`."""
    return "mock" if app_leg == "mock" else "dev"


@pytest.fixture(scope="session")
def base_url(app_leg: str) -> str:
    """Return this leg's myapp base URL, overridable via E2E_BASE_URL for the dev leg only.

    The mock-mode legs' whole point is not depending on an externally-managed instance,
    so each always gets its own fixed port instead.
    """
    if app_leg == "dev" and "E2E_BASE_URL" in os.environ:
        return os.environ["E2E_BASE_URL"]
    return f"http://myapp:{_LEG_PORTS[app_leg]}"


@pytest.fixture(scope="session")
def _reset_dev_database(app_mode: str) -> None:
    """Truncate heroes/revisions before the dev leg's tests run.

    Unlike mock's InMemoryRepository (fresh per uvicorn subprocess), dev's real
    Postgres persists across every e2e run against this same devcontainer stack.
    Every role journey creates test heroes, and craftainer_crud.interfaces.base.OwnerScope
    means
    tests/e2e/editor's own cleanup can never actually delete what it creates (a
    known, accepted gap -- see docs/adrs/0011-owner-scoped-crud-example-resource.md),
    so rows leak on every run. Once enough accumulate to push a test's own newly
    created hero past the CRUD list endpoint's default `limit=100`, tests that
    assert their hero shows up in that default list start failing -- starting each
    dev-mode leg (`dev` and `bare`) from empty tables is what prevents that. `revisions` isn't
    FK-linked to
    `heroes` (see craftainer_crud.models.revision's own docstring: it's an intentionally
    append-only audit log that outlives the record it describes), so it needs its
    own truncate rather than relying on `heroes`'s cascade to reach it -- and to
    avoid stale rows colliding with a hero id that `RESTART IDENTITY` lets a later
    run reuse.

    Skipped when E2E_BASE_URL points elsewhere: that mode assumes the target
    environment is managed by someone else, not disposable test data this suite
    owns, so truncating it would be destructive rather than a cleanup.
    """
    if app_mode != "dev" or "E2E_BASE_URL" in os.environ:
        return

    async def _truncate() -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text("TRUNCATE TABLE heroes, revisions RESTART IDENTITY CASCADE")
            )

    # asyncio.run in a thread of its own, not an async fixture: pytest-asyncio's
    # session-scoped async fixtures don't survive app_leg's own session-scoped
    # reparametrization across legs (every later-leg test errors with "coroutine ...
    # setup was never awaited"), and a second dev-mode leg sets up after Playwright's sync
    # API already runs an event loop in this thread, which asyncio.run() refuses to nest
    # in. A dedicated thread has no loop yet; .join() keeps this synchronous.
    thread = threading.Thread(target=asyncio.run, args=(_truncate(),))
    thread.start()
    thread.join()


@pytest.fixture(scope="session", autouse=True)
def _running_app(
    app_leg: str, app_mode: str, base_url: str, _reset_dev_database: None
) -> Generator[AppHandle]:
    """Start the api server for the duration of the e2e run, unless one is
    already up (e.g. under the "FastAPI: api" launch config) or E2E_BASE_URL
    points somewhere this suite doesn't own (dev leg only).
    """
    if app_mode == "dev" and "E2E_BASE_URL" in os.environ:
        yield AppHandle()
        return

    health_url = f"{base_url}/health/live"
    try:
        httpx.get(health_url, timeout=1).raise_for_status()
        yield AppHandle()
        return
    except httpx.HTTPError:
        pass

    port = urlparse(base_url).port
    uvicorn = shutil.which("uvicorn")
    if uvicorn is None:
        raise RuntimeError("uvicorn not found on PATH")
    otlp = OtlpReceiver() if app_leg in _OTLP_LEGS else None
    # The api's stdout (JSON log lines) goes to a file so journeys can read it back.
    log_path = Path(tempfile.mkdtemp(prefix="e2e-api-")) / f"{app_leg}.log"
    mode_env = {
        "MODE": app_mode,
        **({"ALLOW_MOCK_MODE": "1"} if app_mode == "mock" else {}),
        # Generous, not the small production defaults: every e2e test file's own
        # login/bulk-action calls share one real Redis-backed counter (keyed by
        # client address, not per test), and this suite's own traffic volume would
        # otherwise trip app.rate_limit's rate_limit_mock_token/rate_limit_bulk_action
        # defaults well before any test intends to exercise rate limiting itself.
        # The 429 itself is exercised by test_observability_e2e.py's rate-limit journey
        # (a client key of its own, past the 1000/minute budget) and unit-tested in
        # tests/unit/test_rate_limit.py.
        "RATE_LIMIT_MOCK_TOKEN": "1000/minute",
        "RATE_LIMIT_BULK_ACTION": "1000/minute",
        # Short, not the 15s production default: test_hero_events_e2e.py waits for a
        # real keep-alive comment on an idle GET .../events subscription, and doing
        # that at the production interval would make that test needlessly slow.
        "SSE_KEEPALIVE_SECONDS": "0.2",
        **_LEG_ENV[app_leg],
        **(
            {
                # Telemetry goes to the in-process receiver, flushed on short schedules.
                "OTEL_EXPORTER_OTLP_ENDPOINT": otlp.endpoint,
                "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
                "OTEL_SERVICE_NAME": "template-fastapi-e2e",
                "OTEL_METRIC_EXPORT_INTERVAL": "500",
                "OTEL_BSP_SCHEDULE_DELAY": "200",
                "OTEL_BLRP_SCHEDULE_DELAY": "200",
            }
            if otlp is not None
            else {}
        ),
    }
    # 0.0.0.0: must be reachable from the sibling selenium container, not
    # just loopback -- same as the root README's manual startup command.
    # Fixed args plus our own parsed base_url's port, not external input.
    env = {key: value for key, value in os.environ.items() if key not in _LEG_ENV_KEYS}
    log_file = log_path.open("wb")
    process = subprocess.Popen(  # noqa: S603
        [uvicorn, "app.main:app", "--host", "0.0.0.0", "--port", str(port)],  # noqa: S104
        env={**env, **_COVERAGE_SUBPROCESS_ENV, **mode_env},
        stdout=log_file,
    )
    try:
        deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                httpx.get(health_url, timeout=1).raise_for_status()
                break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            process.terminate()
            process.wait(timeout=5)
            raise RuntimeError(
                f"api server did not become healthy within {_STARTUP_TIMEOUT_SECONDS}s"
            )
        yield AppHandle(
            managed=True,
            otlp=otlp,
            log_path=log_path,
            expected_version=_APP_VERSION_BY_LEG.get(app_leg, _INSTALLED_VERSION),
            leg=app_leg,
        )
    finally:
        process.terminate()
        process.wait(timeout=5)
        log_file.close()
        if otlp is not None:
            otlp.close()


@pytest.fixture(scope="session")
def app_handle(_running_app: AppHandle) -> AppHandle:
    """The running api process' handle: OTLP receiver, log file and expected /health/info."""
    return _running_app


@pytest.fixture(scope="session")
def backdate_hero(app_mode: str) -> Callable[[int, datetime], None]:
    """Return a function that sets an existing hero's `created_at` directly via SQL.

    `craftainer_crud.controllers.crud_stats.forecast`'s success path (and its week/month
    bucket-math branches) needs at least two distinct calendar buckets of history
    -- unreachable by simply creating heroes through the API, since every e2e run
    creates its own test data within one real day. This bypasses the API's
    server-controlled `created_at` (see app.views.hero_v2.HeroV2's own docstring:
    it's never client-settable) the same way `_reset_dev_database` above already
    reaches Postgres directly, to fabricate the multi-bucket history those branches
    need. `dev` only: MODE=mock's InMemoryRepository lives inside the running api
    subprocess's own memory, unreachable from this test process -- a test using
    this fixture under the `mock` leg should `pytest.skip` instead (its branches
    still get covered once from the `dev` leg in the same session; coverage is
    combined across both, not measured per-leg -- see pyproject.toml's
    [tool.coverage.run]).
    """
    if app_mode != "dev":

        def _unavailable(hero_id: int, when: datetime) -> None:
            pytest.skip("backdating created_at needs direct DB access -- MODE=dev only")

        return _unavailable

    async def _backdate(hero_id: int, when: datetime) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE heroes SET created_at = :when WHERE id = :id"),
                {"when": when, "id": hero_id},
            )

    def _run(hero_id: int, when: datetime) -> None:
        # Unlike _reset_dev_database (a session fixture, run before anything else
        # starts an event loop), this runs from inside a test body -- Playwright's
        # sync API already drives its own asyncio event loop in this thread by
        # then, and asyncio.run() refuses to nest inside a loop that's already
        # running. A dedicated thread has no loop of its own yet, so asyncio.run()
        # there is safe; .join() still makes this call synchronous from the
        # caller's point of view.
        thread = threading.Thread(target=asyncio.run, args=(_backdate(hero_id, when),))
        thread.start()
        thread.join()

    return _run


@pytest.fixture(scope="session")
def browser(playwright: Playwright) -> Generator[Browser]:
    """Connect to the selenium container's Chromium over CDP.

    No browser binaries are installed in this container -- see
    tests/e2e/README.md for why the browser itself lives in a separate
    container that Playwright connects to remotely instead.
    """
    selenium_url = os.environ.get("E2E_SELENIUM_URL", "http://selenium:4444")
    driver = Remote(command_executor=f"{selenium_url}/wd/hub", options=ChromeOptions())
    try:
        cdp_url = str(driver.capabilities["se:cdp"])
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        try:
            yield browser
        finally:
            browser.close()
    finally:
        driver.quit()


def _dev_realm_client_roles() -> dict[str, list[str]]:
    """Map each dev-realm username to its client roles on the "api" client.

    Read out of realm-export.json rather than hand-duplicated here, so the mock
    leg's role mapping can never drift from the real dev-realm one it stands in for.
    """
    realm = json.loads(_REALM_EXPORT_PATH.read_text())
    return {user["username"]: user["clientRoles"]["api"] for user in realm["users"]}


@pytest.fixture(scope="session")
def access_token(app_mode: str, base_url: str) -> Callable[[str], str]:
    """Return a function that logs in `username` and returns their access token.

    Under `dev`, a real password-grant login against the dev-realm Keycloak. Under
    `mock`, POST /mock/token with the same username's client roles from
    realm-export.json, so every test can call access_token("editor") unchanged
    regardless of mode.
    """
    if app_mode == "mock":
        client_roles = _dev_realm_client_roles()

        def _fetch_mock(username: str) -> str:
            response = httpx.post(
                f"{base_url}/mock/token",
                json={"sub": username, "roles": client_roles[username]},
                timeout=10,
            )
            response.raise_for_status()
            return response.json()["access_token"]  # type: ignore[no-any-return]

        return _fetch_mock

    settings = get_settings()

    def _fetch(username: str) -> str:
        """Log in `username` (dev-realm password == username) and return their access token."""
        # The in-network address (the discovery URL's realm), not settings.oidc_token_url:
        # that one is the browser-facing localhost URL Swagger UI uses, unreachable from here.
        token_url = (
            settings.discovery_url.removesuffix("/.well-known/openid-configuration")
            + "/protocol/openid-connect/token"
        )
        response = httpx.post(
            token_url,
            data={
                "grant_type": "password",
                "client_id": "test-cli",
                "username": username,
                "password": username,
            },
            timeout=10,
        )
        response.raise_for_status()
        return response.json()["access_token"]  # type: ignore[no-any-return]

    return _fetch
