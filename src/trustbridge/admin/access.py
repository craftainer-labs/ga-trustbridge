"""`TenantAccess` over Postgres, and the tenant rows the read routes list (ADR 0031, 0033)."""

from contextlib import AbstractContextManager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.rbac import TenantRef
from trustbridge.tenancy import TenantInfo, list_tenants, tenant_context

_ONE = text("SELECT id, host, org FROM tb_list_tenants() WHERE id = :id")


class SqlTenantAccess:
    """What `require_role` needs: the tenant row by id and the tenant context.

    `get_tenant` reads through `tb_list_tenants()` in its own short transaction with no context
    and closes it before returning (ADR 0031: the `after_begin` hook only fires when a
    transaction begins, so the check must not leave one open for the handler).
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """Read tenants through `factory`."""
        self._factory = factory

    async def get_tenant(self, tenant_id: UUID) -> TenantRef | None:
        """The tenant's id, host and org login, or None."""
        with tenant_context(None):
            async with self._factory() as session, session.begin():
                row = (await session.execute(_ONE, {"id": tenant_id})).first()
        return None if row is None else TenantRef(row.id, row.host, row.org or None)

    def tenant_context(self, tenant_id: UUID) -> AbstractContextManager[None]:
        """`trustbridge.tenancy.tenant_context`."""
        return tenant_context(tenant_id)


class NoTenants:
    """`TenantAccess` for `MODE=mock` without a database: no tenant exists."""

    async def get_tenant(self, tenant_id: UUID) -> TenantRef | None:
        """Always None."""
        return None

    def tenant_context(self, tenant_id: UUID) -> AbstractContextManager[None]:
        """The real context (harmless; no tenant is ever resolved)."""
        return tenant_context(tenant_id)


def tenant_ref(info: TenantInfo) -> TenantRef:
    """The RBAC view of a tenant row (an empty org login, before the first sync, is None)."""
    return TenantRef(info.id, info.host, info.org or None)


async def all_tenants(factory: async_sessionmaker[AsyncSession]) -> list[TenantInfo]:
    """Every tenant, ordered by id."""
    return sorted(await list_tenants(factory), key=lambda t: str(t.id))
