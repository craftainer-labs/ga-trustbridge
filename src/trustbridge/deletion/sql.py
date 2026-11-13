"""`DeletionStore` over Postgres: the definer functions of migration `5d7a2c8e1b94`.

Each transition and its audit events run in ONE session transaction, opened inside the
deletion's `tenant_context` (ADR 0031's insert policy admits the context tenant), so a
`completed` event inserts even though `tb_delete_tenant` removed the tenant row in the same
transaction (NG6h-4a ruling 2). A failed transition rolls its events back with it.
`TB003` (lease lost) and `TB004` (terminal or invalid) both raise `LeaseLost`: stop now.
"""

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.audit import AuditEvent, append, with_trace
from trustbridge.deletion.model import DeletionRecord, Lease, LeaseLost
from trustbridge.tenancy.context import tenant_context

_LEASE_SQLSTATES = frozenset({"TB003", "TB004"})
_CLAIM = text("SELECT * FROM tb_claim_tenant_deletion(:holder, :ttl)")
_RENEW = text("SELECT tb_renew_tenant_deletion(:id, :holder, :fence, :ttl)")
_RELEASE = text("SELECT tb_release_tenant_deletion(:id, :holder, :fence)")
_RETRY = text("SELECT tb_retry_tenant_deletion(:id, :holder, :fence, :error, :delay)")
_ADVANCE = text(
    "SELECT tb_advance_tenant_deletion(:id, :holder, :fence, :to_state, "
    "CAST(:cursor AS jsonb), CAST(:counts AS jsonb), CAST(:skipped AS jsonb))"
)
_FAIL = text("SELECT tb_fail_tenant_deletion(:id, :holder, :fence, :reason)")
_DELETE_TENANT = text("SELECT tb_delete_tenant(:tenant_id, :holder, :fence)")
_FINISH = text(
    "SELECT tb_finish_tenant_deletion(:id, :holder, :fence, "
    "CAST(:counts AS jsonb), CAST(:skipped AS jsonb))"
)


def _json(value: Any) -> Any:  # noqa: ANN401 -- jsonb arrives decoded or as text
    return json.loads(value) if isinstance(value, str) else value


def _dump(value: Mapping[str, Any] | Sequence[Any] | None) -> str | None:
    return None if value is None else json.dumps(value)


def _record(row: RowMapping) -> DeletionRecord:
    return DeletionRecord(
        id=row["id"],
        tenant_id=row["tenant_id"],
        host=row["host"],
        installation_id=row["installation_id"],
        delete_platform_resources=row["delete_platform_resources"],
        state=row["state"],
        step_cursor=_json(row["step_cursor"]),
        counts=_json(row["counts"]),
        skipped=_json(row["skipped"]),
        fence=row["fence"],
        attempts=row["attempts"],
    )


def _lease_args(lease: Lease) -> dict[str, Any]:
    return {"id": lease.id, "holder": lease.holder, "fence": lease.fence}


class SqlDeletionStore:
    """Writes the deletion record through its `SECURITY DEFINER` functions only."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """Use `factory` for one short transaction per call."""
        self._factory = factory

    @asynccontextmanager
    async def _tx(self, tenant_id: UUID | None) -> AsyncIterator[AsyncSession]:
        """One transaction in `tenant_id`'s context; `TB003` and `TB004` become `LeaseLost`."""
        try:
            with tenant_context(tenant_id):
                async with self._factory() as session, session.begin():
                    yield session
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) in _LEASE_SQLSTATES:
                raise LeaseLost from error
            raise

    async def _events(self, session: AsyncSession, events: Sequence[AuditEvent]) -> None:
        for event in events:
            await append(session, with_trace(event))

    async def claim(self, holder: str, ttl_seconds: int) -> DeletionRecord | None:
        """Take one runnable deletion's lease (`SKIP LOCKED`, fence bumped), or None."""
        async with self._tx(None) as session:
            row = (await session.execute(_CLAIM, {"holder": holder, "ttl": ttl_seconds})).mappings()
            first = row.first()
        return None if first is None else _record(first)

    async def renew(self, lease: Lease, ttl_seconds: int) -> None:
        """Extend the lease."""
        async with self._tx(None) as session:
            await session.execute(_RENEW, {**_lease_args(lease), "ttl": ttl_seconds})

    async def release(self, lease: Lease) -> None:
        """Free the lease."""
        async with self._tx(None) as session:
            await session.execute(_RELEASE, _lease_args(lease))

    async def retry(self, lease: Lease, error: str, delay_seconds: int) -> int:
        """Record the error, count the attempt, back off and free the lease."""
        async with self._tx(None) as session:
            attempts = (
                await session.execute(
                    _RETRY, {**_lease_args(lease), "error": error, "delay": delay_seconds}
                )
            ).scalar_one()
        return int(attempts)

    async def advance(  # noqa: PLR0913 -- the function's own arguments
        self,
        lease: Lease,
        tenant_id: UUID,
        to_state: str,
        *,
        cursor: Mapping[str, Any] | None,
        counts: Mapping[str, int],
        skipped: Sequence[Mapping[str, Any]],
        events: Sequence[AuditEvent],
    ) -> None:
        """Move along (or save the cursor) and write `events`, in one transaction."""
        params = {
            **_lease_args(lease),
            "to_state": to_state,
            "cursor": _dump(cursor),
            "counts": _dump(counts),
            "skipped": _dump(skipped),
        }
        async with self._tx(tenant_id) as session:
            await session.execute(_ADVANCE, params)
            await self._events(session, events)

    async def finish(
        self,
        lease: Lease,
        record: DeletionRecord,
        *,
        counts: Mapping[str, int],
        skipped: Sequence[Mapping[str, Any]],
        events: Sequence[AuditEvent],
    ) -> None:
        """Delete the `tenants` row (fenced), write `done` and `events`, in one transaction."""
        async with self._tx(record.tenant_id) as session:
            await session.execute(
                _DELETE_TENANT, {**_lease_args(lease), "tenant_id": record.tenant_id}
            )
            await session.execute(
                _FINISH, {**_lease_args(lease), "counts": _dump(counts), "skipped": _dump(skipped)}
            )
            await self._events(session, events)

    async def fail(
        self, lease: Lease, tenant_id: UUID, reason: str, events: Sequence[AuditEvent]
    ) -> None:
        """Write the terminal `failed` state and `events`."""
        async with self._tx(tenant_id) as session:
            await session.execute(_FAIL, {**_lease_args(lease), "reason": reason})
            await self._events(session, events)
