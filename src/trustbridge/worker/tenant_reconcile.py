"""The tenant reconcile body (NG5r-b, ADR 0029 "eligibility is re-evaluated at each reconcile").

For one tenant, in this order (option A: it reconciles what exists and creates no new scope):

1. `resume_operation` for each of the tenant's open operations (what `resume_all` does for every
   tenant, narrowed to this one);
2. `ensure_scope` for each stored `adapter_credentials` row, with the tenant's stored scopes as the
   siblings, which re-evaluates the OIDC eligibility of each (a protected environment that
   changed, a scope that now shares its environment);
3. `sweep_orphans` for each adapter the tenant has credentials for, then `sweep_parked_values`.

It is idempotent: a request can run twice (a reclaimed claim, ADR 0028 invariant 10) and a second
run finds nothing to change. The admin-disable gate (`SqlTenantGate`, ADR 0033 invariant 19) is
enforced by the core on every call, so a tenant disabled mid-run stops the body quietly. The core's
store observer already audits each credential change (actor `reconciler`), so the body adds no
audit event of its own.

`extend` is called before each write step (it re-stamps the claim, see `ReconcileQueue.extend`);
when the claim is gone it raises `ClaimLostError` and the body stops before that step. A scope whose
step fails does not stop the others; the body raises `ReconcileIncompleteError` at the end so the
request is retried after the claim TTL, up to `TENANT_RECONCILE_MAX_ATTEMPTS` times.
"""

import logging
from collections.abc import Awaitable, Mapping, Sequence
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.adapters.core import (
    AdapterRegistry,
    CoreDeps,
    CredentialCore,
    OperationFailedError,
    RetryableOperationError,
    TenantDisabledError,
)
from trustbridge.adapters.interface import AdapterError, PlatformAdapter
from trustbridge.adapters.sql_store import SqlCredentialStore
from trustbridge.adapters.store import CredentialStore, LeaseLostError, TenantResolver
from trustbridge.audit.actions import SystemActor
from trustbridge.audit.credentials import CredentialAuditObserver
from trustbridge.audit.emit import AuditWriter
from trustbridge.github.writer import GitHubEnvironmentInspector, GitHubOutputWriter
from trustbridge.tenancy.gate import SqlTenantGate
from trustbridge.worker.lease import LeaseBusy, LeaseLost
from trustbridge.worker.reconcile_queue import Extend

if TYPE_CHECKING:  # runtime imports this module to compose the body
    from trustbridge.worker.runtime import Core

logger = logging.getLogger(__name__)

# What one scope's step may fail with without stopping the others (the platform, GitHub, the lease).
_STEP_ERRORS = (
    RetryableOperationError,
    OperationFailedError,
    AdapterError,
    LeaseBusy,
    LeaseLost,
    LeaseLostError,
)


class ReconcileIncompleteError(Exception):
    """Some steps failed; the others ran. The request is retried after the claim TTL."""


class CredentialReconciler:
    """The `TenantReconciler` over the credential core."""

    def __init__(
        self,
        core: CredentialCore,
        store: CredentialStore,
        tenants: TenantResolver,
        adapters: Sequence[str],
    ) -> None:
        """Bind the core, the store it reads, the tenant resolver and the enabled adapters."""
        self._core = core
        self._store = store
        self._tenants = tenants
        self._adapters = tuple(adapters)

    async def reconcile(self, tenant_id: UUID, extend: Extend) -> None:
        """Run the steps; `ClaimLostError` from `extend` aborts before the next write step."""
        tenant = await self._tenants.get_tenant(tenant_id)
        failed = 0
        try:
            failed += await self._resume(tenant_id, extend)
            by_adapter = {
                a: await self._store.list_credentials(tenant_id, a) for a in self._adapters
            }
            scopes = list(dict.fromkeys(c.scope for rows in by_adapter.values() for c in rows))
            for adapter, rows in by_adapter.items():
                for row in rows:
                    await extend()
                    failed += await self._guarded(
                        self._core.ensure_scope(tenant, adapter, row.scope, scopes),
                        tenant_id,
                        f"{adapter} ensure_scope",
                    )
            for adapter, rows in by_adapter.items():
                if rows:
                    await extend()
                    failed += await self._guarded(
                        self._core.sweep_orphans(tenant, adapter),
                        tenant_id,
                        f"{adapter} sweep_orphans",
                    )
            await extend()
            failed += await self._guarded(
                self._core.sweep_parked_values(), tenant_id, "sweep_parked_values"
            )
        except TenantDisabledError:
            logger.info(
                "tenant reconcile stopped: tenant is disabled", extra={"tenant": str(tenant_id)}
            )
            return
        if failed:
            msg = f"{failed} reconcile step(s) failed"
            raise ReconcileIncompleteError(msg)

    async def _resume(self, tenant_id: UUID, extend: Extend) -> int:
        """Resume the tenant's open operations; the number that failed."""
        failed = 0
        for op in await self._store.list_open_operations():
            if op.tenant_id != tenant_id:
                continue
            await extend()
            failed += await self._guarded(
                self._core.resume_operation(op.id), tenant_id, f"{op.adapter} resume_operation"
            )
        return failed

    async def _guarded(self, step: Awaitable[object], tenant_id: UUID, name: str) -> int:
        """Await `step`; 1 when it failed with a platform, GitHub or lease error, else 0."""
        try:
            await step
        except _STEP_ERRORS as exc:
            # the type only: an adapter's message is not for the log of a loop
            logger.warning(
                "tenant reconcile step failed: %s (%s)",
                name,
                type(exc).__name__,
                extra={"tenant": str(tenant_id)},
            )
            return 1
        return 0


def build_tenant_reconciler(
    core: Core,
    sessions: async_sessionmaker[AsyncSession] | None,
    classes: Mapping[str, type[PlatformAdapter]],
) -> CredentialReconciler | None:
    """The reconcile body for the reconciler process, or None without a database.

    Composed as the api composes its core (`admin.credentials.build_runtime`, ADR 0029): the
    enabled adapter classes over the tenants' connection data (read through the existing registry),
    the SQL credential store whose saves append the credential events (actor `reconciler`), the
    lease manager, parked values, the GitHub output writer and environment inspector, and the
    admin-disable gate.
    """
    if sessions is None:
        return None
    observer = CredentialAuditObserver(AuditWriter(sessions), SystemActor.RECONCILER)
    store = SqlCredentialStore(sessions, observer)
    gateway = core.runtime.gateway
    deps = CoreDeps(
        registry=AdapterRegistry(classes, core.connections),
        store=store,
        values=core.parked,
        leases=core.leases,
        writer=GitHubOutputWriter(gateway),
        inspector=GitHubEnvironmentInspector(gateway),
        tenants=core.tenants,
        orphans=observer,
        gate=SqlTenantGate(sessions),
    )
    return CredentialReconciler(CredentialCore(deps), store, core.tenants, sorted(classes))
