"""Reading and writing `audit_events` (ADR 0032). Callers own the transaction.

* `append` inserts in the caller's transaction (so the row commits with the change it records),
  never uses `RETURNING`, and lets the database assign `occurred_at`, `recorded_at` and `seq`.
* `list_events` and `export_ndjson` read a tenant's events directly (RLS applies, and the tenant
  is also filtered explicitly) or, with `instance_scope=True`, every tenant's through
  `tb_audit_list()`, which is for an instance-scoped auditor only: this layer does not check roles.
* `get_event` reads one event by id through `tb_audit_get()` (instance scope, like `tb_audit_list`).
* `purge_batch` and `purge_expired` delete only through `tb_audit_purge()`.
"""

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.audit.model import (
    ActorType,
    AuditEvent,
    AuditPage,
    AuditQuery,
    AuditRecord,
    Outcome,
)

_MAX_SEQ = 9223372036854775807
DEFAULT_PURGE_BATCH = 1000
MAX_PURGE_BATCH = 10000  # the ceiling `tb_audit_purge` enforces
DEFAULT_EXPORT_PAGE = 500
_COLUMNS = (
    "id, seq, occurred_at, recorded_at, tenant_id, actor_type, actor_sub, actor_iss, "
    "on_behalf_of::text AS on_behalf_of, host(source_ip) AS source_ip, action, resource_type, "
    'resource_id, outcome, "before"::text AS "before", "after"::text AS "after", request_id, '
    "trace_id, schema_version"
)
_INSERT = text(
    "INSERT INTO audit_events (id, tenant_id, actor_type, actor_sub, actor_iss, on_behalf_of, "
    'source_ip, action, resource_type, resource_id, outcome, "before", "after", request_id, '
    "trace_id, schema_version) VALUES (CAST(:id AS uuid), CAST(:tenant_id AS uuid), :actor_type, "
    ":actor_sub, :actor_iss, CAST(CAST(:on_behalf_of AS text) AS jsonb), "
    "CAST(CAST(:source_ip AS text) AS inet), :action, "
    ":resource_type, :resource_id, :outcome, CAST(CAST(:before AS text) AS jsonb), "
    "CAST(CAST(:after AS text) AS jsonb), :request_id, :trace_id, :schema_version)"
)
_LIST_ALL = text(
    f"SELECT {_COLUMNS} FROM tb_audit_list("  # noqa: S608 -- constants only
    "CAST(:tenant_id AS uuid), CAST(:after_seq AS bigint), CAST(:before_seq AS bigint), "
    "CAST(:since AS timestamptz), CAST(:until AS timestamptz), CAST(:actor_sub AS text), "
    "CAST(:action AS text), CAST(:resource_type AS text), CAST(:resource_id AS text), "
    "CAST(:outcome AS text), CAST(:lag AS double precision), :descending, :limit)"
)
_GET_ONE = text(f"SELECT {_COLUMNS} FROM tb_audit_get(CAST(:id AS uuid))")  # noqa: S608 -- constants only
_LAG_BOUND = text(
    f"SELECT COALESCE(min(seq), {_MAX_SEQ}) FROM audit_events WHERE tenant_id = :tenant_id "  # noqa: S608 -- constants only
    "AND recorded_at > clock_timestamp() - make_interval(secs => CAST(:lag AS double precision))"
)
_PURGE = text(
    "SELECT tb_audit_purge(CAST(:before AS timestamptz), CAST(:min_days AS integer), "
    "CAST(:batch AS integer))"
)


def _dump(value: dict[str, Any] | None) -> str | None:
    return None if value is None else json.dumps(value)


def _load(value: str | None) -> dict[str, Any] | None:
    return None if value is None else json.loads(value)


async def append(session: AsyncSession, event: AuditEvent) -> UUID:
    """Insert `event` in the session's transaction and return its id. Does not commit.

    The row's `tenant_id` must be null or the session's tenant context (the insert policy).
    """
    await session.execute(
        _INSERT,
        {
            "id": event.id,
            "tenant_id": event.tenant_id,
            "actor_type": event.actor_type.value,
            "actor_sub": event.actor_sub,
            "actor_iss": event.actor_iss,
            "on_behalf_of": _dump(event.on_behalf_of),
            "source_ip": event.source_ip,
            "action": event.action.value,
            "resource_type": event.resource_type,
            "resource_id": event.resource_id,
            "outcome": event.outcome.value,
            "before": _dump(event.before),
            "after": _dump(event.after),
            "request_id": event.request_id,
            "trace_id": event.trace_id,
            "schema_version": event.schema_version,
        },
    )
    return event.id


def _record(row: RowMapping) -> AuditRecord:
    return AuditRecord(
        id=UUID(str(row["id"])),
        seq=row["seq"],
        occurred_at=row["occurred_at"],
        recorded_at=row["recorded_at"],
        tenant_id=None if row["tenant_id"] is None else UUID(str(row["tenant_id"])),
        actor_type=ActorType(row["actor_type"]),
        actor_sub=row["actor_sub"],
        actor_iss=row["actor_iss"],
        on_behalf_of=_load(row["on_behalf_of"]),
        source_ip=row["source_ip"],
        action=row["action"],
        resource_type=row["resource_type"],
        resource_id=row["resource_id"],
        outcome=Outcome(row["outcome"]),
        before=_load(row["before"]),
        after=_load(row["after"]),
        request_id=row["request_id"],
        trace_id=row["trace_id"],
        schema_version=row["schema_version"],
    )


async def _fetch(
    session: AsyncSession, query: AuditQuery, tenant_id: UUID | None, limit: int
) -> list[AuditRecord]:
    """Up to `limit` records in `seq` order (direction per the query)."""
    if tenant_id is None:
        result = await session.execute(
            _LIST_ALL,
            {
                "tenant_id": query.tenant_id,
                "after_seq": query.after_seq,
                "before_seq": query.before_seq,
                "since": query.since,
                "until": query.until,
                "actor_sub": query.actor_sub,
                "action": query.action,
                "resource_type": query.resource_type,
                "resource_id": query.resource_id,
                "outcome": None if query.outcome is None else query.outcome.value,
                "lag": query.lag_seconds,
                "descending": query.descending,
                "limit": limit,
            },
        )
        return [_record(row) for row in result.mappings()]
    # Tenant scope: a plain SELECT (RLS applies too), with every condition a fixed fragment.
    conds = ["tenant_id = :tenant_id"]
    params: dict[str, Any] = {"tenant_id": tenant_id, "limit": limit}
    fixed = (
        ("after_seq", "seq > :after_seq"),
        ("before_seq", "seq < :before_seq"),
        ("since", "occurred_at >= :since"),
        ("until", "occurred_at < :until"),
        ("actor_sub", "actor_sub = :actor_sub"),
        ("action", "action = :action"),
        ("resource_type", "resource_type = :resource_type"),
        ("resource_id", "resource_id = :resource_id"),
    )
    for name, cond in fixed:
        value = getattr(query, name)
        if value is not None:
            conds.append(cond)
            params[name] = value
    if query.outcome is not None:
        conds.append("outcome = :outcome")
        params["outcome"] = query.outcome.value
    if query.lag_seconds is not None:
        bound = await session.execute(
            _LAG_BOUND, {"tenant_id": tenant_id, "lag": query.lag_seconds}
        )
        conds.append("seq < :lag_limit")
        params["lag_limit"] = bound.scalar_one()
    order = "DESC" if query.descending else "ASC"
    sql = (
        f"SELECT {_COLUMNS} FROM audit_events WHERE {' AND '.join(conds)} "  # noqa: S608 -- fixed fragments only
        f"ORDER BY seq {order} LIMIT :limit"
    )
    result = await session.execute(text(sql), params)
    return [_record(row) for row in result.mappings()]


def _check_scope(tenant_id: UUID | None, instance_scope: bool) -> None:
    """Instance scope is an explicit opt-in: a missing tenant must never widen a read."""
    if tenant_id is None and not instance_scope:
        raise ValueError("tenant_id is required unless instance_scope=True")
    if tenant_id is not None and instance_scope:
        raise ValueError("instance_scope=True cannot be combined with a tenant_id")


async def list_events(
    session: AsyncSession,
    query: AuditQuery,
    *,
    tenant_id: UUID | None = None,
    instance_scope: bool = False,
) -> AuditPage:
    """One page. `tenant_id` set: that tenant's events (open the session in its context).

    `instance_scope=True` (with `tenant_id=None`) reads all tenants, including null-tenant
    events, through `tb_audit_list()`; call it only for an instance-scoped auditor.
    `query.tenant_id` then narrows the read; in tenant scope it must be unset or equal
    `tenant_id`. `tenant_id=None` without `instance_scope=True`, or both together, raises
    `ValueError` before any query.
    """
    _check_scope(tenant_id, instance_scope)
    if tenant_id is not None and query.tenant_id not in (None, tenant_id):
        raise ValueError("query.tenant_id conflicts with the tenant scope")
    rows = await _fetch(session, query, tenant_id, query.limit + 1)
    more = len(rows) > query.limit
    items = rows[: query.limit]
    return AuditPage(items=items, next_seq=items[-1].seq if more else None)


async def get_event(session: AsyncSession, event_id: UUID) -> AuditRecord | None:
    """The event with this id (any tenant, null-tenant events included), or None.

    Reads through `tb_audit_get()`, so it is for an instance-scoped auditor only; like
    `list_events(instance_scope=True)` this layer does not check roles. Does not commit.
    """
    row = (await session.execute(_GET_ONE, {"id": event_id})).mappings().first()
    return None if row is None else _record(row)


async def export_ndjson(  # noqa: PLR0913 - the two scope arguments must stay separate keywords
    session: AsyncSession,
    query: AuditQuery,
    *,
    tenant_id: UUID | None = None,
    instance_scope: bool = False,
    lag_seconds: float,
    page_size: int = DEFAULT_EXPORT_PAGE,
) -> AsyncIterator[bytes]:
    """Stream the events as NDJSON lines in ascending `seq`, resumable with `query.after_seq`.

    Only events recorded more than `lag_seconds` ago are returned (and none after the first
    one that is not), so resuming from the last `seq` never skips a late commit. The caller
    writes the export's own audit event and sets the response's content type
    (`application/x-ndjson`). Scope rules are `list_events`'s; a violation raises `ValueError`
    on the first iteration, before any query.
    """
    current = replace(query, descending=False, lag_seconds=lag_seconds, limit=page_size)
    while True:
        page = await list_events(
            session, current, tenant_id=tenant_id, instance_scope=instance_scope
        )
        for record in page.items:
            yield record.to_ndjson_line()
        if page.next_seq is None:
            return
        current = replace(current, after_seq=page.next_seq)


async def purge_batch(
    session: AsyncSession,
    *,
    before: datetime,
    min_days: int,
    batch_size: int = DEFAULT_PURGE_BATCH,
) -> int:
    """Delete one batch of events older than `before` via `tb_audit_purge`; returns the count.

    The function refuses a cut-off newer than `now() - min_days` and writes a summary event when
    it deleted anything. The floor is a typo guard supplied by the caller from
    `AUDIT_RETENTION_MIN_DAYS`, not a boundary against a compromised app role. Does not commit.
    """
    result = await session.execute(
        _PURGE, {"before": before, "min_days": min_days, "batch": batch_size}
    )
    return int(result.scalar_one())


async def purge_expired(
    factory: async_sessionmaker[AsyncSession],
    *,
    retention_days: int,
    min_days: int,
    batch_size: int = DEFAULT_PURGE_BATCH,
    now: datetime | None = None,
) -> int:
    """Purge everything older than `retention_days` in short transactions, one per batch.

    `retention_days == 0` means never purge (nothing is touched). Returns the total deleted.
    The reconciler runs it as a singleton duty (EB-6).
    """
    if retention_days == 0:
        return 0
    if not 1 <= batch_size <= MAX_PURGE_BATCH:
        raise ValueError(f"batch_size must be between 1 and {MAX_PURGE_BATCH}")
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    total = 0
    while True:
        async with factory() as session, session.begin():
            deleted = await purge_batch(
                session, before=cutoff, min_days=min_days, batch_size=batch_size
            )
        total += deleted
        if deleted < batch_size:
            return total
