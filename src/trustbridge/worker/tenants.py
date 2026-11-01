"""Tenant resolution: `(host, installation_id)` to the surrogate tenant UUID (ADR 0028).

`set_tenant` sets the per-transaction RLS setting (`trustbridge.tenant_id`) that ADR 0031's
policies key on; it must be called at the start of every transaction that touches a
tenant-owned table (the setting is transaction-local, so it cannot be set once for a session).
"""

import uuid
from typing import Protocol

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.worker.models import Tenant

TENANT_SETTING = "trustbridge.tenant_id"


class TenantResolver(Protocol):
    """Maps an installation on a host to its tenant id, creating it on first sight."""

    async def resolve(self, host: str, installation_id: int) -> uuid.UUID:
        """The tenant id for `(host, installation_id)`."""
        ...


async def set_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Set the transaction-local RLS setting for `tenant_id` on `session`."""
    await session.execute(
        text("SELECT set_config(:name, :value, true)"),
        {"name": TENANT_SETTING, "value": str(tenant_id)},
    )


class PgTenantResolver:
    """`tenants` in Postgres."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Bind the session factory."""
        self._sf = session_factory

    async def resolve(self, host: str, installation_id: int) -> uuid.UUID:
        """Insert-if-absent then read, so concurrent first sights agree on one id."""
        ins = (
            pg_insert(Tenant)
            .values(host=host, installation_id=installation_id)
            .on_conflict_do_nothing(constraint="uq_tenants_host_inst")
        )
        async with self._sf() as session, session.begin():
            await session.execute(ins)
            row = await session.execute(
                select(Tenant.id).where(Tenant.host == host, Tenant.installation_id == installation_id)
            )
            return row.scalar_one()


class InMemoryTenantResolver:
    """Process-local resolver for `MODE=mock` and unit tests."""

    def __init__(self) -> None:
        """Start empty."""
        self._ids: dict[tuple[str, int], uuid.UUID] = {}

    async def resolve(self, host: str, installation_id: int) -> uuid.UUID:
        """A stable id per pair."""
        return self._ids.setdefault((host, installation_id), uuid.uuid4())
