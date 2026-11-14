"""The adapter-side port of a tenant deletion's drain step (ADR 0034 step 1; NG6h-7a).

`SqlWritersPort.quiesce` finds what could still write for the tenant and returns how many writers
are active; the drain step (`deletion.drain.DrainStep`) calls it until two passes in a row find
none. A pass:

1. resumes every non-terminal `credential_operations` row of the tenant through
   `CredentialCore.resume_operation` (the normal rules: an operation finishes, or its cleanup
   runs and it ends `failed` or `abandoned` with its parked values removed). A resume that fails
   for a platform, GitHub or lease reason is logged by class only and leaves the row open;
2. counts the tenant's operations that are still open, and its `scope_leases` that are still
   held (a lease is held while `expires_at` is in the future).

Webhook deliveries are not scanned: an admin-disabled tenant's handlers skip its deliveries, so
they stay pending until the database step marks them `deleted`, and `tb_delete_tenant` refuses
while one is left. This module imports nothing from `deletion`.
"""

import logging
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.adapters.core import (
    CredentialCore,
    OperationFailedError,
    RetryableOperationError,
    TenantDisabledError,
)
from trustbridge.adapters.interface import AdapterError
from trustbridge.adapters.models import CredentialOperationRow
from trustbridge.adapters.store import TERMINAL_STATUSES, LeaseLostError
from trustbridge.tenancy.context import tenant_context
from trustbridge.worker.lease import LeaseBusy, LeaseLost
from trustbridge.worker.models import ScopeLease

logger = logging.getLogger(__name__)

_TERMINAL = [status.value for status in TERMINAL_STATUSES]
_RESUME_ERRORS = (
    RetryableOperationError,
    OperationFailedError,
    TenantDisabledError,
    AdapterError,
    LeaseBusy,
    LeaseLost,
    LeaseLostError,
)


class SqlWritersPort:
    """`WritersPort` over the tenant's operation and lease rows and the credential core."""

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], credentials: CredentialCore
    ) -> None:
        """Read rows through `sessions`; resume operations through `credentials`."""
        self._sf = sessions
        self._credentials = credentials

    async def _open_operations(self, tenant_id: UUID) -> list[UUID]:
        with tenant_context(tenant_id):
            async with self._sf() as session:
                rows = await session.scalars(
                    select(CredentialOperationRow.id).where(
                        CredentialOperationRow.tenant_id == tenant_id,
                        CredentialOperationRow.status.not_in(_TERMINAL),
                    )
                )
                return list(rows)

    async def _held_leases(self, tenant_id: UUID) -> int:
        with tenant_context(tenant_id):
            async with self._sf() as session:
                held = await session.scalar(
                    select(func.count())
                    .select_from(ScopeLease)
                    .where(
                        ScopeLease.tenant_id == tenant_id,
                        ScopeLease.expires_at > func.clock_timestamp(),
                    )
                )
                return int(held or 0)

    async def quiesce(self, tenant_id: UUID) -> int:
        """Try to settle the tenant's open operations, then count what is still active."""
        for operation_id in await self._open_operations(tenant_id):
            try:
                await self._credentials.resume_operation(operation_id)
            except _RESUME_ERRORS as error:
                # the class only: an adapter's message is not for a log
                logger.warning(
                    "deletion drain: an operation did not settle (%s)",
                    type(error).__name__,
                    extra={"tenant": str(tenant_id)},
                )
        return len(await self._open_operations(tenant_id)) + await self._held_leases(tenant_id)
