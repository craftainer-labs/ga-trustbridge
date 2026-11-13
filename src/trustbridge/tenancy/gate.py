"""Whether a tenant is admin-disabled, read for the worker and the adapter core (ADR 0033).

Lives in `tenancy` because both the worker (deliveries) and the api (the credential core behind
the rotate route) use it: `SqlTenantGate` is the worker's `TenantGate` and the adapter core's
`OperationGate` over the definer functions.
"""

from uuid import UUID

from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.adapters.store import OperationKind
from trustbridge.tenancy.context import tenant_context
from trustbridge.tenancy.standing import TenantStanding

_DISABLED_BY_INSTALLATION = text(
    "SELECT admin_disabled_at IS NOT NULL "
    "FROM tb_get_tenant(tb_resolve_tenant(CAST(:host AS text), CAST(:installation AS bigint)))"
)
_STANDING = text(
    "SELECT t.id, t.admin_disabled_at IS NOT NULL, t.deleting "
    "FROM tb_get_tenant(tb_resolve_tenant(CAST(:host AS text), CAST(:installation AS bigint))) t"
)
# A `done` deletion of the key, or a `database`-step one whose tenant row is already gone
# (the same rule as `tb_upsert_tenant` and `SqlTenants`): the key is tombstoned.
_TOMBSTONE = text(
    "SELECT EXISTS (SELECT 1 FROM tenant_deletions d "
    "WHERE d.host = CAST(:host AS text) AND d.installation_id = CAST(:installation AS bigint) "
    "AND (d.state = 'done' OR (d.state = 'database' AND NOT EXISTS ("
    "SELECT 1 FROM tb_get_tenant(tb_resolve_tenant(CAST(:host AS text), "
    "CAST(:installation AS bigint)))))))"
)
_DISABLED_BY_ID = text("SELECT admin_disabled_at IS NOT NULL FROM tb_get_tenant(CAST(:id AS uuid))")


class SqlTenantGate:
    """`TenantGate` (and the adapter core's `OperationGate`) over the definer functions.

    One short read per call, in no tenant context (`tb_get_tenant` and `tb_resolve_tenant` are
    the unscoped paths to `tenants`, ADR 0031). An unknown tenant is not disabled. Whether a
    deletion's own delete and revoke operations pass a disabled tenant is NG6h's: it replaces
    the `kind` test in `allows` (ADR 0033 "Tenant disable").
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Read through `session_factory`."""
        self._sf = session_factory

    async def _flag(self, statement: TextClause, params: dict[str, object]) -> bool:
        with tenant_context(None):
            async with self._sf() as session, session.begin():
                return bool((await session.execute(statement, params)).scalar())

    async def disabled(self, host: str, installation_id: int) -> bool:
        """True when the tenant of `(host, installation_id)` is admin-disabled."""
        return await self._flag(
            _DISABLED_BY_INSTALLATION, {"host": host, "installation": installation_id}
        )

    async def standing(self, host: str, installation_id: int) -> TenantStanding:
        """The tenant of `(host, installation_id)`: its id, disable flag and deletion refusal.

        Two reads in one transaction, in no tenant context. A key with a finished deletion has
        no tenant row, so its refusal is `tombstone`; a tenant with a non-terminal deletion is
        `deleting`.
        """
        params = {"host": host, "installation": installation_id}
        with tenant_context(None):
            async with self._sf() as session, session.begin():
                row = (await session.execute(_STANDING, params)).first()
                if row is not None:
                    refusal = "deleting" if row[2] else None
                    return TenantStanding(row[0], bool(row[1]), refusal)
                tombstoned = bool((await session.execute(_TOMBSTONE, params)).scalar())
        return TenantStanding(refusal="tombstone" if tombstoned else None)

    async def allows(self, tenant_id: UUID, kind: OperationKind | None) -> bool:
        """True when a new adapter operation of `kind` may start for the tenant."""
        del kind  # NG6h: a deletion's own delete and revoke operations are the exception
        return not await self._flag(_DISABLED_BY_ID, {"id": tenant_id})
