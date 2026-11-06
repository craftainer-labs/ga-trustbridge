"""`TenantAccess` over Postgres, and the tenant rows the read routes list (ADR 0031, 0033)."""

from contextlib import AbstractContextManager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.rbac import Principal, TenantRef
from trustbridge.tenancy import TenantInfo, tenant_context

_ONE = text("SELECT id, host, org FROM tb_get_tenant(:id)")
_PAGE = text(
    "SELECT id, host, installation_id, org, org_id, state, admin_disabled_at, deleting "
    "FROM tb_list_tenants() "
    "WHERE (CAST(:after AS uuid) IS NULL OR id > CAST(:after AS uuid)) "
    "AND (CAST(:every AS boolean) OR (lower(host), lower(org)) IN "
    "(SELECT * FROM unnest(CAST(:hosts AS text[]), CAST(:orgs AS text[])))) "
    "ORDER BY id LIMIT :limit"
)


class SqlTenantAccess:
    """What `require_role` needs: the tenant row by id and the tenant context.

    `get_tenant` reads through `tb_get_tenant()` in its own short transaction with no context
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


async def tenants_page(
    factory: async_sessionmaker[AsyncSession],
    principal: Principal,
    *,
    after: UUID | None,
    limit: int,
) -> list[TenantInfo]:
    """Up to `limit` tenants the principal holds some grant on, ordered by id, after `after`.

    Every role confers `viewer`, so any grant reaches a tenant: an instance grant reaches all,
    an organisation grant the tenants with that (case-insensitive) host and org. The filter and
    the keyset run in the database, so the cost is the page, not the tenant table. A principal
    with no grants gets none without a query.
    """
    if not principal.grants:
        return []
    every = any(grant.instance for grant in principal.grants)
    pairs = sorted(
        {(g.host, g.org) for g in principal.grants if g.host is not None and g.org is not None}
    )
    with tenant_context(None):
        async with factory() as session, session.begin():
            rows = (
                await session.execute(
                    _PAGE,
                    {
                        "after": after,
                        "every": every,
                        "hosts": [host for host, _ in pairs],
                        "orgs": [org for _, org in pairs],
                        "limit": limit,
                    },
                )
            ).all()
    return [TenantInfo(*row) for row in rows]
