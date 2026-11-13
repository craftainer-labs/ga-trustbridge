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
# One statement, so the tenant row and the deletions are read from one snapshot (`tb_get_tenant` is
# STABLE). A `done` deletion of the key, or a `database`-step one whose tenant row is already gone
# (the same rule as `tb_upsert_tenant` and `SqlTenants`), is a tombstone. The outer `one` row makes
# an unknown tenant come back as a row of nulls, so there is always exactly one row.
_STANDING = text(
    "SELECT t.id, COALESCE(t.admin_disabled_at IS NOT NULL, false), COALESCE(t.deleting, false), "
    "EXISTS (SELECT 1 FROM tenant_deletions d "
    "WHERE d.host = CAST(:host AS text) AND d.installation_id = CAST(:installation AS bigint) "
    "AND (d.state = 'done' OR (d.state = 'database' AND t.id IS NULL))) "
    "FROM (SELECT 1) one LEFT JOIN LATERAL "
    "tb_get_tenant(tb_resolve_tenant(CAST(:host AS text), CAST(:installation AS bigint))) t ON true"
)
# `deleting` is true while a non-terminal `tenant_deletions` row of the tenant exists (the same
# `tb_get_tenant` snapshot as the disable flag).
_STATE_BY_ID = text(
    "SELECT admin_disabled_at IS NOT NULL, deleting FROM tb_get_tenant(CAST(:id AS uuid))"
)


class SqlTenantGate:
    """`TenantGate` (and the adapter core's `OperationGate`) over the definer functions.

    One short read per call, in no tenant context (`tb_get_tenant` and `tb_resolve_tenant` are
    the unscoped paths to `tenants`, ADR 0031). An unknown tenant is not disabled. A disabled
    tenant refuses every new adapter call except a `delete` operation (a credential's delete and
    revoke, the only destructive kind) while a non-terminal deletion of the tenant exists: that
    is the deletion's own platform step (ADR 0034 B10, ADR 0033 "Tenant disable"). A `done`,
    `failed` or `cancelled` deletion no longer admits it.
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

        One statement in one transaction, in no tenant context. A key with a finished deletion
        has no tenant row, so its refusal is `tombstone`; a tenant with a non-terminal deletion
        is `deleting`. A tombstone is checked first, as `SqlTenants` does, and carries no id.
        """
        params = {"host": host, "installation": installation_id}
        with tenant_context(None):
            async with self._sf() as session, session.begin():
                row = (await session.execute(_STANDING, params)).one()
        if row[3]:
            return TenantStanding(refusal="tombstone")
        return TenantStanding(row[0], bool(row[1]), "deleting" if row[2] else None)

    async def allows(self, tenant_id: UUID, kind: OperationKind | None) -> bool:
        """True when a new adapter operation of `kind` may start for the tenant.

        An enabled (or unknown) tenant admits everything. A disabled one admits only
        `OperationKind.DELETE`, and only while a non-terminal deletion of it exists.
        """
        with tenant_context(None):
            async with self._sf() as session, session.begin():
                row = (await session.execute(_STATE_BY_ID, {"id": tenant_id})).first()
        if row is None or not row[0]:
            return True
        return kind is OperationKind.DELETE and bool(row[1])
