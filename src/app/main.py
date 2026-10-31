"""FastAPI app entrypoint: wires up settings, migrations, routers, and lifespan."""

import asyncio
import gc
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from importlib import metadata

import debugpy
from alembic.config import Config
from craftainer_crud.controllers.crud_manifest import api_catalog_router, crud_manifest_router
from craftainer_crud.controllers.crud_router import ROUTER_VERSION
from craftainer_crud.interfaces.cache import StatsCache
from craftainer_observability.analytics import shutdown as analytics_shutdown
from craftainer_observability.health.router import build_health_router
from craftainer_observability.logs import configure_logging
from craftainer_observability.middleware import RequestIDMiddleware
from craftainer_observability.otel import (
    configure_telemetry,
    instrument_app,
    instrument_engine,
)
from craftainer_observability.profiling import install_profiling
from fastapi import APIRouter, FastAPI

from alembic import command
from app.analytics import AnalyticsContextMiddleware
from app.cache import RedisStatsCache, run_invalidation
from app.config import get_settings
from app.controllers import (
    audit,
    mock,
)
from app.crud_1 import router as crud_v1_router
from app.crud_1.heroes import heroes_v2
from app.crud_1.wiring import manifest_registry, stats_cache
from app.db import engine
from app.health_checks import close_health_checks, get_health_registry
from app.http_headers import add_security_headers
from app.oidc import prefetch_jwks
from app.problem_details import register_problem_handlers
from app.well_known import build_well_known_router

settings = get_settings()
configure_logging()


def _run_migrations() -> None:
    """Apply any pending Alembic migrations against the configured database.

    Blocking (Alembic's async recipe runs its own asyncio.run internally), so the
    caller must run this off the event loop -- see lifespan below. `alembic.ini` is
    resolved relative to the current working directory: the repo root in the
    devcontainer and under pytest, /app in the runner image (see the root
    Dockerfile's runner stage, which copies alembic.ini/alembic/ there).
    """
    command.upgrade(Config("alembic.ini"), "head")


@asynccontextmanager
async def _stats_cache_invalidation(cache: StatsCache) -> AsyncIterator[None]:
    """Keep `cache` invalidated by Hero's CRUD events while serving; close it afterwards.

    The invalidation task is cancelled and awaited first, then a Redis-backed cache's
    client is closed (the in-process cache has nothing to close). The resource name
    matches the routers' `resource="hero"` (both Hero versions share one event stream).
    """
    task = asyncio.create_task(
        run_invalidation(
            cache,
            heroes_v2.get_hero_event_source(),
            resource="hero",
        )
    )
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        if isinstance(cache, RedisStatsCache):
            await cache.aclose()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Migrate, warm the JWKS cache, start cache invalidation; undo it in reverse on shutdown.

    MODE=mock skips migrations entirely -- there's no database to migrate against
    (see craftainer_crud.repositories.memory), as does RUN_MIGRATIONS_ON_STARTUP=false
    (migrations then run as a separate deploy step). `# pragma: no branch` below is for
    tests/e2e specifically: its live processes always run with the flag at its default and
    one of them in a migrating mode, so the other branch can never run there --
    tests/unit/test_main.py exercises it directly and still counts toward its own gate.

    Once startup work is done (and `GC_FREEZE` isn't false), `gc.collect(); gc.freeze()`
    moves everything allocated so far out of the cyclic GC's reach. Shutdown ends with
    `close_health_checks()` (the Redis check's client), a final analytics flush and
    `engine.dispose()`, so clients
    and pooled connections close cleanly.
    """
    if settings.run_migrations_on_startup and settings.mode != "mock":  # pragma: no branch
        await asyncio.to_thread(_run_migrations)
    await prefetch_jwks()
    try:
        async with AsyncExitStack() as stack:
            # `# pragma: no branch`: both e2e legs enable the cache (Redis and in-process).
            if stats_cache is not None:  # pragma: no branch
                await stack.enter_async_context(_stats_cache_invalidation(stats_cache))
            if settings.gc_freeze:
                gc.collect()
                gc.freeze()
            yield
    finally:
        try:
            await close_health_checks()
        finally:
            try:
                # Final flush of queued analytics events; joins the flusher thread (up to
                # 5 s), so off the event loop. Never raises.
                await asyncio.to_thread(analytics_shutdown)
            finally:
                await engine.dispose()


def _service_version() -> str:
    """`APP_VERSION` if set, else this distribution's installed version (as `/health/info`)."""
    return os.environ.get("APP_VERSION") or metadata.version("template-fastapi")


def _docs_path(path: str, mode: str) -> str | None:
    """`path` for the interactive docs/schema routes, or None (route off) in production."""
    return None if mode == "production" else path


def _api_catalog_router(mode: str) -> APIRouter:
    """The api-catalog router; `service-desc` only where `/openapi.json` is actually served.

    Production serves no OpenAPI document (see `_docs_path`), so advertising it would leave
    a dangling link.
    """
    return api_catalog_router(
        manifest_url="/api/_crud/manifest",
        openapi_url=_docs_path("/openapi.json", mode),
    )


def _enable_debugger() -> None:
    """Start listening for a remote debugger attach on :5678 (MODE=dev only).

    Doesn't call wait_for_client(): startup must not block when nobody attaches.
    When the process is already launched under VS Code's own debugpy (the
    "FastAPI: api" launch config in .vscode/launch.json), debugpy is already
    injected and a second listen() raises RuntimeError -- expected, not an error,
    so it's swallowed here.
    """
    with suppress(RuntimeError):
        # 127.0.0.1, not 0.0.0.0: VS Code's remote-attach reaches this through the
        # devcontainer's own port forwarding (see .devcontainer/compose.yml), which
        # connects to the container's loopback interface -- binding every interface
        # would also accept a debugger connection from elsewhere on the container's
        # network, with no ALLOW_MOCK_MODE-style second gate the way MODE=mock has.
        debugpy.listen(("127.0.0.1", 5678))


def _configure_debugger(mode: str) -> None:
    """Enable the remote debugger when the given mode is "dev".

    `# pragma: no branch` below is for tests/e2e specifically: its one live
    process is always MODE=dev, so the other branch can never run there --
    tests/unit/test_main.py exercises it directly and still counts toward its
    own 95% gate.
    """
    if mode == "dev":  # pragma: no branch
        _enable_debugger()


def _mount_mode_specific_routers(app: FastAPI, mode: str) -> None:
    """Mount routers that only make sense for the given mode.

    Currently just POST /mock/token (app.controllers.mock) for MODE=mock -- see
    app.controllers.mock's docstring for why it's mode-gated. The mock branch is
    `# pragma: no cover` for tests/e2e specifically: its one live process is
    always MODE=dev, so this branch can never run there -- tests/unit/test_main.py
    exercises it directly and still counts toward its own 95% gate.
    """
    if mode == "mock":  # pragma: no cover
        app.include_router(mock.router, prefix="/mock")


_configure_debugger(settings.mode)
configure_telemetry()

_production = settings.mode == "production"

app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan,
    debug=settings.mode == "dev",
    docs_url=_docs_path("/docs", settings.mode),
    redoc_url=_docs_path("/redoc", settings.mode),
    openapi_url=_docs_path("/openapi.json", settings.mode),
    swagger_ui_init_oauth={
        "usePkceWithAuthorizationCodeGrant": True,
        "clientId": settings.oidc_client_id,
    },
)
register_problem_handlers(app)
add_security_headers(app)

app.include_router(
    build_health_router(
        get_health_registry,
        include_info=settings.health_info_enabled,
        info_package="template-fastapi",
    ),
    prefix="/health",
)
app.include_router(crud_v1_router, prefix=f"/crud/v{ROUTER_VERSION}")
app.include_router(audit.router, prefix="/audit")
# The CRUD manifest (GET /api/_crud/manifest, filtered to what the caller may read) and the
# RFC 9727 api-catalog that points clients at it. The catalog route carries its absolute
# /.well-known/api-catalog path itself (the well-known location is fixed by the RFC), so it is
# the one mount with no segment of its own -- see crud_1/README.md's "Every mount names its
# own segment".
app.include_router(
    crud_manifest_router(
        manifest_registry,
        service_name=settings.app_name,
        service_version=_service_version(),
        stats_ttl=settings.stats_cache_ttl_seconds,
        # The public issuer's discovery URL (what a client can reach), not settings.discovery_url.
        oidc_discovery_url=f"{settings.expected_issuer.rstrip('/')}/.well-known/openid-configuration",
        prefix="",
    ),
    prefix="/api/_crud",
)
app.include_router(_api_catalog_router(settings.mode), prefix="")
# RFC 9728 oauth-protected-resource, and security.txt when SECURITY_TXT_CONTACT is set.
app.include_router(build_well_known_router(settings), prefix="")
_mount_mode_specific_routers(app, settings.mode)

# Middleware order, outermost first: OTel server span, RequestIDMiddleware, profiling,
# security headers. Starlette makes the most recently added middleware outermost, so the
# request-ID and profiling middleware are added after add_security_headers above. OTel
# doesn't follow that rule: FastAPIInstrumentor.instrument_app() wraps
# build_middleware_stack so its span encloses the whole user-middleware stack wherever it
# is called (checked in the installed opentelemetry-instrumentation-fastapi). That is
# what lets RequestIDMiddleware find an active server span to tag. The profile response
# bypasses the security headers on purpose: pyinstrument's HTML needs inline scripts.
# Both instrument_* calls are no-ops unless OTEL_EXPORTER_OTLP_ENDPOINT is set.
install_profiling(app, enabled=settings.profiling_enabled, production=_production)
# The analytics context (GPC/DNT/consent signals for track(), ADR 0027) sits inside the
# request-ID middleware: it needs nothing from it and wraps only the app itself.
app.add_middleware(AnalyticsContextMiddleware)
app.add_middleware(RequestIDMiddleware)
instrument_app(app)
instrument_engine(engine)
