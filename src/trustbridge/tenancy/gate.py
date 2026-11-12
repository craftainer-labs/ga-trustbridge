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

_DISABLED_BY_INSTALLATION = text(
    "SELECT admin_disabled_at IS NOT NULL "
    "FROM tb_get_tenant(tb_resolve_tenant(CAST(:host AS text), CAST(:installation AS bigint)))"
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

    async def allows(self, tenant_id: UUID, kind: OperationKind | None) -> bool:
        """True when a new adapter operation of `kind` may start for the tenant."""
        del kind  # NG6h: a deletion's own delete and revoke operations are the exception
        return not await self._flag(_DISABLED_BY_ID, {"id": tenant_id})
