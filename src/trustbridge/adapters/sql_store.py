"""Postgres implementation of `CredentialStore`.

Every guarded write is a single statement that carries the lease's `guard()` predicate
(`INSERT ... SELECT ... WHERE <guard> ON CONFLICT DO UPDATE`), so a holder whose lease expired
or was taken over writes zero rows and gets `LeaseLostError` (invariant 10). The credential row
and the operation row are written in one transaction.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast
from uuid import UUID

from sqlalchemy import ColumnElement, Result, Table, delete, func, literal, select
from sqlalchemy.dialects.postgresql import Insert, insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from trustbridge.adapters import codec
from trustbridge.adapters.interface import ResourceState
from trustbridge.adapters.models import (
    AdapterCredentialRow,
    AdapterResourceRow,
    CredentialOperationRow,
)
from trustbridge.adapters.store import (
    TERMINAL_STATUSES,
    CredentialKind,
    Lease,
    LeaseLostError,
    Operation,
    OperationKind,
    OperationStatus,
    StoredCredential,
)

SessionFactory = Callable[[], AsyncSession]

_TERMINAL = [s.value for s in TERMINAL_STATUSES]


def _guarded_upsert(
    table: Table,
    values: Mapping[str, Any],
    pk: Sequence[str],
    guard: ColumnElement[bool],
) -> Insert:
    """Build `INSERT ... SELECT <values> WHERE <guard> ON CONFLICT (pk) DO UPDATE`."""
    cols = list(values)
    selected = [
        (v if isinstance(v, ColumnElement) else literal(v, table.c[c].type)).label(c)
        for c, v in values.items()
    ]
    stmt = insert(table).from_select(cols, select(*selected).where(guard))
    updates = {c: stmt.excluded[c] for c in cols if c not in pk}
    return stmt.on_conflict_do_update(index_elements=list(pk), set_=updates)


def _table(model: type[Any]) -> Table:
    return cast("Table", model.__table__)


def _rowcount(result: Result[Any]) -> int:
    return cast("CursorResult[Any]", result).rowcount


def _credential_from_row(row: AdapterCredentialRow) -> StoredCredential:
    return StoredCredential(
        tenant_id=row.tenant_id,
        adapter=row.adapter,
        scope_key=row.scope_key,
        scope=codec.scope_from_json(dict(row.scope)),
        kind=cast("CredentialKind | None", row.kind),
        state=codec.state_from_json(dict(row.state)),
        outputs=codec.outputs_from_json([dict(o) for o in row.outputs]),
    )


def _operation_from_row(row: CredentialOperationRow) -> Operation:
    return Operation(
        id=row.id,
        tenant_id=row.tenant_id,
        adapter=row.adapter,
        scope_key=row.scope_key,
        kind=OperationKind(row.kind),
        status=OperationStatus(row.status),
        oidc_offered=row.oidc_offered,
        base_state=codec.state_from_json(dict(row.base_state)) if row.base_state else None,
        base_kind=cast("CredentialKind | None", row.base_kind),
        attempts=row.attempts,
        error=row.error,
        manual_action=row.manual_action,
    )


class SqlCredentialStore:
    """`CredentialStore` over the `adapter_*` and `credential_operations` tables."""

    def __init__(self, session_factory: SessionFactory) -> None:
        """Use `session_factory` for a short-lived session per call."""
        self._sf = session_factory

    async def get_credential(
        self, tenant_id: UUID, adapter: str, scope_key: str
    ) -> StoredCredential | None:
        """Return the credential row, or None."""
        async with self._sf() as session:
            row = await session.get(AdapterCredentialRow, (tenant_id, adapter, scope_key))
            return _credential_from_row(row) if row else None

    async def list_credentials(self, tenant_id: UUID, adapter: str) -> Sequence[StoredCredential]:
        """Return every credential row of the tenant for one adapter."""
        async with self._sf() as session:
            rows = await session.scalars(
                select(AdapterCredentialRow).where(
                    AdapterCredentialRow.tenant_id == tenant_id,
                    AdapterCredentialRow.adapter == adapter,
                )
            )
            return [_credential_from_row(r) for r in rows]

    async def get_open_operation(
        self, tenant_id: UUID, adapter: str, scope_key: str
    ) -> Operation | None:
        """Return the scope's non-terminal operation, or None."""
        async with self._sf() as session:
            row = await session.scalar(
                select(CredentialOperationRow).where(
                    CredentialOperationRow.tenant_id == tenant_id,
                    CredentialOperationRow.adapter == adapter,
                    CredentialOperationRow.scope_key == scope_key,
                    CredentialOperationRow.status.not_in(_TERMINAL),
                )
            )
            return _operation_from_row(row) if row else None

    async def get_operation(self, operation_id: UUID) -> Operation | None:
        """Return an operation by id, or None."""
        async with self._sf() as session:
            row = await session.get(CredentialOperationRow, operation_id)
            return _operation_from_row(row) if row else None

    async def list_open_operations(self) -> Sequence[Operation]:
        """Return every non-terminal operation."""
        async with self._sf() as session:
            rows = await session.scalars(
                select(CredentialOperationRow).where(
                    CredentialOperationRow.status.not_in(_TERMINAL)
                )
            )
            return [_operation_from_row(r) for r in rows]

    async def save(
        self, lease: Lease, credential: StoredCredential, operation: Operation | None
    ) -> None:
        """Upsert the credential row and the operation row in one transaction, guarded."""
        guard = lease.guard()
        cred_values: dict[str, Any] = {
            "tenant_id": credential.tenant_id,
            "adapter": credential.adapter,
            "scope_key": credential.scope_key,
            "scope": codec.scope_to_json(credential.scope),
            "kind": credential.kind,
            "state": codec.state_to_json(credential.state),
            "outputs": codec.outputs_to_json(credential.outputs),
            "updated_at": func.now(),
        }
        async with self._sf() as session, session.begin():
            result = await session.execute(
                _guarded_upsert(
                    _table(AdapterCredentialRow),
                    cred_values,
                    ["tenant_id", "adapter", "scope_key"],
                    guard,
                )
            )
            if _rowcount(result) != 1:
                raise LeaseLostError
            if operation is not None:
                op_values: dict[str, Any] = {
                    "id": operation.id,
                    "tenant_id": operation.tenant_id,
                    "adapter": operation.adapter,
                    "scope_key": operation.scope_key,
                    "kind": operation.kind.value,
                    "status": operation.status.value,
                    "oidc_offered": operation.oidc_offered,
                    "base_state": (
                        codec.state_to_json(operation.base_state) if operation.base_state else None
                    ),
                    "base_kind": operation.base_kind,
                    "attempts": operation.attempts,
                    "error": operation.error,
                    "manual_action": operation.manual_action,
                    "updated_at": func.now(),
                }
                op_result = await session.execute(
                    _guarded_upsert(
                        _table(CredentialOperationRow),
                        op_values,
                        ["id"],
                        guard,
                    )
                )
                if _rowcount(op_result) != 1:
                    raise LeaseLostError

    async def count_slots(self, tenant_id: UUID, adapter: str) -> int:
        """Count current and previous slots (in-flight ones included)."""
        total = 0
        for cred in await self.list_credentials(tenant_id, adapter):
            total += sum(1 for s in (cred.state.current, cred.state.previous) if s is not None)
        return total

    async def known_names(self, tenant_id: UUID, adapter: str) -> frozenset[str]:
        """Return the names of every slot; an open operation's new slot is in its credential row."""
        names: set[str] = set()
        for cred in await self.list_credentials(tenant_id, adapter):
            for slot in (cred.state.current, cred.state.previous):
                if slot is not None:
                    names.add(slot.name)
        return frozenset(names)

    async def get_resource(
        self, tenant_id: UUID, adapter: str, resource_key: str
    ) -> ResourceState | None:
        """Return a resource's state, or None."""
        async with self._sf() as session:
            row = await session.get(AdapterResourceRow, (tenant_id, adapter, resource_key))
            return codec.resource_from_json(dict(row.state)) if row else None

    async def put_resource(
        self,
        lease: Lease,
        tenant_id: UUID,
        adapter: str,
        resource_key: str,
        state: ResourceState | None,
    ) -> None:
        """Store (or with None, forget) a resource's state, guarded by the lease."""
        async with self._sf() as session, session.begin():
            if state is None:
                await session.execute(
                    delete(AdapterResourceRow).where(
                        AdapterResourceRow.tenant_id == tenant_id,
                        AdapterResourceRow.adapter == adapter,
                        AdapterResourceRow.resource_key == resource_key,
                        lease.guard(),
                    )
                )
                return
            values: dict[str, Any] = {
                "tenant_id": tenant_id,
                "adapter": adapter,
                "resource_key": resource_key,
                "state": codec.resource_to_json(state),
                "updated_at": func.now(),
            }
            result = await session.execute(
                _guarded_upsert(
                    _table(AdapterResourceRow),
                    values,
                    ["tenant_id", "adapter", "resource_key"],
                    lease.guard(),
                )
            )
            if _rowcount(result) != 1:
                raise LeaseLostError
