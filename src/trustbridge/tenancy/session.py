"""Tenant-scoped sessions and the iteration used by background duties (ADR 0031)."""

import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from opentelemetry import metrics
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.tenancy.context import tenant_context

logger = logging.getLogger(__name__)
sweep_failures = metrics.get_meter("trustbridge").create_counter(
    "trustbridge.tenants.sweep_failures",
    description="Tenants whose per-tenant sweep body raised (the loop continued)",
)
_LIST_TENANTS = text(
    "SELECT id, host, installation_id, org, org_id, state, admin_disabled_at, deleting, "
    "admin_disabled_by, admin_disable_reason "
    "FROM tb_list_tenants()"
)
_GET_TENANT = text(
    "SELECT id, host, installation_id, org, org_id, state, admin_disabled_at, deleting, "
    "admin_disabled_by, admin_disable_reason "
    "FROM tb_get_tenant(:id)"
)


@dataclass(frozen=True, slots=True)
class TenantInfo:
    """One row of `tb_list_tenants()`."""

    id: UUID
    host: str
    installation_id: int
    org: str
    org_id: int
    state: str
    admin_disabled_at: datetime | None
    deleting: bool
    admin_disabled_by: str | None = None
    admin_disable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class SweepResult:
    """The outcome of `for_each_tenant`."""

    succeeded: int
    failed: tuple[UUID, ...]


@asynccontextmanager
async def tenant_session(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: UUID
) -> AsyncIterator[AsyncSession]:
    """A session opened inside `tenant_context(tenant_id)`, so its transactions are scoped to it."""
    with tenant_context(tenant_id):
        async with session_factory() as session:
            yield session


async def list_tenants(session_factory: async_sessionmaker[AsyncSession]) -> list[TenantInfo]:
    """Every tenant, read through `tb_list_tenants()` in one short transaction with no context."""
    with tenant_context(None):
        async with session_factory() as session, session.begin():
            rows = (await session.execute(_LIST_TENANTS)).all()
    return [TenantInfo(*row) for row in rows]


async def get_tenant(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: UUID
) -> TenantInfo | None:
    """One tenant by id through `tb_get_tenant()` (a primary-key lookup), or None.

    Like `list_tenants`: one short transaction with no context, closed before returning.
    """
    with tenant_context(None):
        async with session_factory() as session, session.begin():
            row = (await session.execute(_GET_TENANT, {"id": tenant_id})).first()
    return None if row is None else TenantInfo(*row)


async def iterate_tenants(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[UUID]:
    """Yield each tenant id once, with that tenant as the current tenant while it is yielded.

    The list is read first (a transaction closed before any work starts). The context is reset
    when the loop asks for the next tenant, so one tenant's work never runs under another's
    context. Close the generator deterministically (`contextlib.aclosing`), or use
    `for_each_tenant`, which does and also isolates failures.
    """
    for tenant in await list_tenants(session_factory):
        with tenant_context(tenant.id):
            yield tenant.id


async def for_each_tenant(
    session_factory: async_sessionmaker[AsyncSession],
    work: Callable[[UUID], Awaitable[object]],
) -> SweepResult:
    """Run `work(tenant_id)` for every tenant inside its context; a failure does not stop others.

    A failing tenant is logged and counted (`trustbridge.tenants.sweep_failures`) and the loop
    goes on, so one tenant cannot starve the rest; the next pass retries it. Cancellation
    propagates.
    """
    succeeded = 0
    failed: list[UUID] = []
    async with aclosing(iterate_tenants(session_factory)) as tenants:
        async for tenant_id in tenants:
            try:
                await work(tenant_id)
            except Exception:
                logger.exception("tenant sweep failed", extra={"tenant_id": str(tenant_id)})
                sweep_failures.add(1)
                failed.append(tenant_id)
            else:
                succeeded += 1
    return SweepResult(succeeded, tuple(failed))
