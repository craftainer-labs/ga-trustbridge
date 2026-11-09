"""What `app.state.admin` holds: the collaborators the admin routes use (built in `main`)."""

from dataclasses import dataclass
from typing import Protocol

from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.admin import metrics
from trustbridge.admin.adapters import AdapterCatalog
from trustbridge.audit.emit import AuditWriter
from trustbridge.github.availability import HostStatus
from trustbridge.github.ports import ReconcileScheduler
from trustbridge.ratelimit import FixedWindowLimiter, WindowCounter
from trustbridge.settings import DEFAULT_ADMIN_MAX_BODY_BYTES, TrustbridgeSettings

DEFAULT_EXPORT_PAGE = 500
DEFAULT_MAX_BODY_BYTES = DEFAULT_ADMIN_MAX_BODY_BYTES


class HostStatuses(Protocol):
    """The answering replica's view of every configured host (`HostMonitor`)."""

    def statuses(self) -> dict[str, HostStatus]:
        """Each host's state and reason."""
        ...


@dataclass(frozen=True, slots=True)
class AdminLimits:
    """The four buckets of ADR 0033 decision 5."""

    read: FixedWindowLimiter
    mutation: FixedWindowLimiter
    export: FixedWindowLimiter
    anonymous: FixedWindowLimiter


@dataclass(frozen=True, slots=True)
class AdminRuntime:
    """Collaborators of the admin routes. `session_factory` and `writer` are None in mock mode.

    `adapters` is the enabled-adapter catalog for `GET /tenants/{t}/adapters` (None: none).
    `max_body_bytes` bounds a request body (`ADMIN_MAX_BODY_BYTES`). `scheduler` records a
    reconcile request (`POST /tenants/{t}/reconcile`; None: the route answers 503).
    """

    limits: AdminLimits
    hosts: HostStatuses
    session_factory: async_sessionmaker[AsyncSession] | None
    writer: AuditWriter | None
    export_lag_seconds: float
    export_page_size: int = DEFAULT_EXPORT_PAGE
    adapters: AdapterCatalog | None = None
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    scheduler: ReconcileScheduler | None = None


def build_limits(settings: TrustbridgeSettings, counter: WindowCounter) -> AdminLimits:
    """The limiters from `ADMIN_RATE_LIMIT_*` over `counter` (key prefix `tb:rl:admin:`)."""

    def make(name: str, requests: int) -> FixedWindowLimiter:
        return FixedWindowLimiter(
            counter=counter,
            enabled=settings.admin_rate_limit_enabled,
            requests=requests,
            window_seconds=settings.admin_rate_limit_window_seconds,
            timeout_ms=settings.admin_rate_limit_timeout_ms,
            exempt=list(settings.admin_rate_limit_exempt_cidrs),
            prefix=f"tb:rl:admin:{name}",
            rejected=metrics.rate_limit_rejected,
            failopen=metrics.rate_limit_failopen,
        )

    return AdminLimits(
        read=make("read", settings.admin_rate_limit_read_requests),
        mutation=make("mutation", settings.admin_rate_limit_mutation_requests),
        export=make("export", settings.admin_rate_limit_export_requests),
        anonymous=make("anon", settings.admin_rate_limit_anonymous_requests),
    )


def get_admin_runtime(app: FastAPI) -> AdminRuntime | None:
    """The runtime on `app.state.admin`, if installed."""
    runtime = getattr(app.state, "admin", None)
    return runtime if isinstance(runtime, AdminRuntime) else None


def admin_runtime(request: Request) -> AdminRuntime:
    """Dependency: the runtime; absent means the app is misconfigured."""
    runtime = get_admin_runtime(request.app)
    if runtime is None:
        raise RuntimeError("app.state.admin is not set")
    return runtime
