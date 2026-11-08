"""The trustbridge api entrypoint: `uvicorn trustbridge.main:app --no-proxy-headers ...`.

Imports the template app (`app.main:app`: health, auth, problem details, security headers,
telemetry, lifespan), mounts the webhook router, installs uvicorn's `ProxyHeadersMiddleware`
with `TRUSTED_PROXIES` and **wraps the template's lifespan** by replacing
`app.router.lifespan_context` (ADR 0028 "Lifespan"). Nothing in `src/app/` is edited.

The wrapper: validated settings (already applied at import: invalid settings stop the
process), the schema gate (background; the api reports not ready until current, stays alive,
exits non-zero only after `SCHEMA_WAIT_SECONDS`), the per-host startup checks (a failed check
marks the host `unavailable` and never stops the process), a recheck task every
`HOST_RECHECK_SECONDS`, then the template's own lifespan inside; trustbridge's resources are
released after the template has shut down. `lifespan_context` is not a documented Starlette
extension point: `install` fails loudly if it is missing, and the smoke test guards it.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from craftainer_observability.health.registry import HealthRegistry
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.health_checks import get_health_registry
from app.main import app as template_app
from trustbridge.adapters.loader import load_adapter_classes
from trustbridge.admin import metrics as admin_metrics
from trustbridge.admin.access import NoTenants, SqlTenantAccess
from trustbridge.admin.adapters import AdapterCatalog
from trustbridge.admin.problems import install_problem_types
from trustbridge.admin.routes import router as admin_router
from trustbridge.admin.runtime import AdminRuntime, build_limits
from trustbridge.audit import AuditDenials, AuditWriter
from trustbridge.audit.throttle import ThrottledDenials
from trustbridge.github.availability import HostChecker
from trustbridge.github.ports import SecretReader
from trustbridge.github.webhook_secrets import WebhookSecretResolver
from trustbridge.rbac import RbacConfig, RbacRuntime
from trustbridge.settings import TrustbridgeSettings, get_trustbridge_settings
from trustbridge.tenancy import budget_left, install_tenant_hook, register_role_check
from trustbridge.webhooks.argocd import router as argocd_router
from trustbridge.webhooks.ingress import ArgocdRuntime, WebhookRuntime
from trustbridge.webhooks.ingress import router as webhook_router
from trustbridge.webhooks.limiter import (
    InMemoryWindowCounter,
    ValkeyWindowCounter,
    WebhookLimiter,
    WindowCounter,
)
from trustbridge.worker.runtime import Core, build_core, open_redis, session_factory_or_none
from trustbridge.worker.schema import register_schema_check

logger = logging.getLogger(__name__)
LOOPBACK_ONLY = "127.0.0.1"
SHUTDOWN_FLUSH_SECONDS = 5.0


def _trusted_hosts(settings: TrustbridgeSettings) -> list[str] | str:
    """`TRUSTED_PROXIES` for `ProxyHeadersMiddleware`; unset (non-production) is loopback only."""
    if settings.trusted_proxies is None:
        return LOOPBACK_ONLY
    return list(settings.trusted_proxies)


@dataclass(slots=True)
class Overrides:
    """Collaborators a test (or NG4) can substitute; every field defaults to the real thing."""

    checker: HostChecker | None = None
    redis: Redis | None = None
    session_factory: async_sessionmaker[AsyncSession] | None = None
    secret_reader: SecretReader | None = None
    health_registry: Callable[[], HealthRegistry] = get_health_registry


def install(
    app: FastAPI, settings: TrustbridgeSettings, overrides: Overrides | None = None
) -> FastAPI:
    """Mount the webhook route, add the proxy middleware and wrap the lifespan (idempotent)."""
    if getattr(app.state, "trustbridge_installed", False):
        return app
    original = getattr(app.router, "lifespan_context", None)
    if original is None:
        raise RuntimeError(
            "the template no longer exposes app.router.lifespan_context; "
            "trustbridge.main cannot wrap its lifespan (ADR 0028)"
        )
    chosen = overrides or Overrides()
    install_tenant_hook()  # ADR 0031: set app.tenant_id per transaction (idempotent)

    @asynccontextmanager
    async def lifespan(inner: FastAPI) -> AsyncIterator[object]:
        async with (
            _trustbridge_resources(inner, settings, chosen),
            original(inner) as state,
        ):
            yield state

    # Starlette refuses to add middleware once the stack is built (a test that already
    # called the template app); dropping the cached stack is safe before serving.
    app.middleware_stack = None
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=_trusted_hosts(settings))
    app.include_router(webhook_router)
    app.include_router(argocd_router)
    app.include_router(admin_router)  # /admin/v1 (ADR 0033); every route declares its guard
    install_problem_types(app)
    # Not a documented Starlette attribute (see the module docstring); the smoke test guards it.
    app.router.lifespan_context = lifespan  # type: ignore[assignment]
    app.state.trustbridge_installed = True
    return app


async def wait_gates(
    core: Core, settings: TrustbridgeSettings, monotonic: Callable[[], float] = time.monotonic
) -> None:
    """Wait for the schema, then check the database role (ADR 0031; the role check needs it).

    Both waits share one `SCHEMA_WAIT_SECONDS` budget counted from here.
    """
    started = monotonic()
    await core.gate.wait_or_exit(
        timeout=settings.schema_wait_seconds, poll=settings.schema_poll_seconds
    )
    await core.roles.background(
        production=settings.production,
        timeout=budget_left(settings.schema_wait_seconds, started, monotonic),
        poll=settings.schema_poll_seconds,
    )


@asynccontextmanager
async def _trustbridge_resources(
    app: FastAPI, settings: TrustbridgeSettings, overrides: Overrides
) -> AsyncIterator[None]:
    """Startup before the template lifespan, shutdown after it (invariants 14-16, 29)."""
    mock = settings.mode == "mock"
    redis = overrides.redis
    owned_redis = None
    if redis is None and not mock:
        redis = owned_redis = open_redis(settings)
    sessions = overrides.session_factory
    if sessions is None:
        sessions = session_factory_or_none(settings)
    core = build_core(
        settings,
        redis=redis,
        session_factory=sessions,
        checker=overrides.checker,
    )
    monitor = core.runtime.monitor
    counter: WindowCounter = (
        InMemoryWindowCounter() if redis is None else ValkeyWindowCounter(redis)
    )
    secrets = WebhookSecretResolver(
        monitor,
        overrides.secret_reader or core.secrets,
        read_timeout=settings.webhook_secret_read_timeout_seconds,
        refresh_min_seconds=settings.webhook_secret_refresh_min_seconds,
    )
    tasks: list[asyncio.Task[None]] = []
    denials: ThrottledDenials | None = None
    try:
        if not mock:
            registry = overrides.health_registry()
            register_schema_check(registry, core.gate)
            register_role_check(registry, core.roles)  # ADR 0031: not ready on a bypassing role
            await core.gate.refresh()
            tasks.append(asyncio.create_task(wait_gates(core, settings)))
        await monitor.check_all()
        await secrets.warm(monitor.names())
        monitor.start(settings.host_recheck_seconds)
        app.state.trustbridge = WebhookRuntime(
            settings=settings,
            monitor=monitor,
            secrets=secrets,
            store=core.store,
            limiter=WebhookLimiter(
                counter=counter,
                enabled=settings.webhook_rate_limit_enabled,
                requests=settings.webhook_rate_limit_requests,
                window_seconds=settings.webhook_rate_limit_window_seconds,
                timeout_ms=settings.webhook_rate_limit_timeout_ms,
                exempt=list(settings.webhook_rate_limit_exempt_cidrs),
            ),
            relay=core.relay,
            argocd=ArgocdRuntime(
                handler=core.runtime.argocd, tenants=core.tenants, configs=core.connections
            ),
        )
        denials = _install_admin(
            app=app, settings=settings, core=core, counter=counter, sessions=sessions, tasks=tasks
        )
        yield
    finally:
        await monitor.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if denials is not None:
            await _final_flush(denials)
        with contextlib.suppress(AttributeError):
            del app.state.trustbridge
        for name in ("rbac", "admin"):
            # Starlette's State raises AttributeError for an unset name (KeyError on some
            # versions); a startup that failed half-way leaves some of them unset.
            with contextlib.suppress(AttributeError, KeyError):
                delattr(app.state, name)
        if owned_redis is not None:
            await owned_redis.aclose()


def _install_admin(  # noqa: PLR0913 - the collaborators main owns
    *,
    app: FastAPI,
    settings: TrustbridgeSettings,
    core: Core,
    counter: WindowCounter,
    sessions: async_sessionmaker[AsyncSession] | None,
    tasks: list[asyncio.Task[None]],
) -> ThrottledDenials | None:
    """Build `app.state.rbac` (with the throttled denial sink) and `app.state.admin`.

    Returns the denial throttle (None without a database) so the lifespan can flush it at shutdown.
    """
    writer = None if sessions is None else AuditWriter(sessions)
    denials = None
    if writer is not None:
        denials = ThrottledDenials(
            AuditDenials(writer),
            counter,
            window_seconds=settings.audit_denied_window_seconds,
            burst=settings.audit_denied_burst,
            timeout_ms=settings.audit_denied_timeout_ms,
            suppressed=admin_metrics.authz_denied_suppressed,
        )
        tasks.append(
            asyncio.create_task(_flush_denials(denials, settings.audit_denied_window_seconds))
        )
    app.state.rbac = RbacRuntime(
        RbacConfig(
            client_id=settings.oidc_client_id,
            separator=settings.rbac_role_separator,
            alias_personas=settings.rbac_alias_template_personas,
        ),
        NoTenants() if sessions is None else SqlTenantAccess(sessions),
        denials=denials,
    )
    app.state.admin = AdminRuntime(
        limits=build_limits(settings, counter),
        hosts=core.runtime.monitor,
        session_factory=sessions,
        writer=writer,
        export_lag_seconds=settings.audit_export_lag_seconds,
        max_body_bytes=settings.admin_max_body_bytes,
        scheduler=core.scheduler,
        # ADR 0029: only the enabled adapters are imported; a bad name stops the process.
        adapters=AdapterCatalog(load_adapter_classes(settings.adapters_enabled), core.secrets),
    )
    return denials


async def _flush_denials(throttle: ThrottledDenials, interval: int) -> None:
    """Write the summary rows of closed denial windows at startup, then once a window.

    The first pass finishes what a previous process left pending in the shared store.
    """
    while True:
        try:
            await throttle.flush()
        except Exception:
            logger.exception("denial summary flush failed")
        await asyncio.sleep(interval)


async def _final_flush(throttle: ThrottledDenials) -> None:
    """One last pass at graceful shutdown (closed windows only); a failure is logged.

    Bounded, so a slow counter store cannot push the shutdown past the container's grace period.
    """
    try:
        async with asyncio.timeout(SHUTDOWN_FLUSH_SECONDS):
            await throttle.flush()
    except Exception:
        logger.exception("denial summary flush at shutdown failed")


def __getattr__(name: str) -> FastAPI:
    """Build `trustbridge.main:app` on first access (uvicorn's `module:attr` lookup).

    Importing this module must not read the environment or mutate the template app, so the
    unit tests can import `install` and `Overrides`; the first access to `app` does both.
    """
    if name != "app":
        raise AttributeError(name)
    return install(template_app, get_trustbridge_settings())
