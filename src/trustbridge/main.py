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
from trustbridge.github.availability import HostChecker
from trustbridge.github.ports import SecretReader
from trustbridge.github.runtime import HostRuntime
from trustbridge.github.webhook_secrets import WebhookSecretResolver
from trustbridge.settings import TrustbridgeSettings, get_trustbridge_settings
from trustbridge.webhooks.argocd import router as argocd_router
from trustbridge.webhooks.ingress import ArgocdRuntime, WebhookRuntime
from trustbridge.webhooks.ingress import router as webhook_router
from trustbridge.webhooks.limiter import (
    InMemoryWindowCounter,
    ValkeyWindowCounter,
    WebhookLimiter,
    WindowCounter,
)
from trustbridge.worker.runtime import build_core, open_redis, session_factory_or_none
from trustbridge.worker.schema import register_schema_check

logger = logging.getLogger(__name__)
LOOPBACK_ONLY = "127.0.0.1"


def _trusted_hosts(settings: TrustbridgeSettings) -> list[str] | str:
    """`TRUSTED_PROXIES` for `ProxyHeadersMiddleware`; unset (non-production) is loopback only."""
    if settings.trusted_proxies is None:
        return LOOPBACK_ONLY
    return list(settings.trusted_proxies)


@dataclass(slots=True)
class Overrides:
    """Collaborators a test (or NG4) can substitute; every field defaults to the real thing."""

    runtime: HostRuntime | None = None
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
    app.router.lifespan_context = lifespan
    app.state.trustbridge_installed = True
    return app


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
        runtime=overrides.runtime,
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
    try:
        if not mock:
            register_schema_check(overrides.health_registry(), core.gate)
            await core.gate.refresh()
            tasks.append(
                asyncio.create_task(
                    core.gate.wait_or_exit(
                        timeout=settings.schema_wait_seconds, poll=settings.schema_poll_seconds
                    )
                )
            )
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
        yield
    finally:
        await monitor.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(AttributeError):
            del app.state.trustbridge
        if owned_redis is not None:
            await owned_redis.aclose()


app: FastAPI = install(template_app, get_trustbridge_settings())
