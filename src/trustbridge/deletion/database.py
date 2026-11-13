"""The database step of a tenant deletion (ADR 0034 step 5; NG6h-4b).

Clears every row trustbridge holds for the tenant except the `tenants` row, which the engine
removes last, through the fenced `tb_delete_tenant`, in the transaction that finishes the record.
One pass is one bounded batch in one transaction, in the tenant's context (ADR 0031), as the
app role: its `tenant_isolation` policies admit `DELETE` for the context tenant and the app role
already holds the table grants, so no `SECURITY DEFINER` function is added (least privilege: the
app role gains nothing). The tables have no foreign keys; the order is the ADR's (in-flight
operations first). `webhook_deliveries` is host-scoped and outside RLS: the tenant's rows keep
their dedupe key (`host`, `delivery_id`, `body_sha256`) and the non-personal `event`, and lose
every field that can hold personal or tenant data: `body`, `headers` (the allow-listed request
headers, which are not read once a row is `deleted`; the column is NOT NULL, so it becomes `{}`)
and `last_error` (handler error text). The row becomes `deleted` (never relayed or handled again).

Every pass starts with a lease check, in the same transaction as the batch: the holder, the
fence and an unexpired lease for a deletion still in the `database` step (a `SELECT`, because the
app role cannot lock `tenant_deletions`). It is a guard, not a lock: a holder that loses the lease in the
instant after the check deletes at most one more batch of rows that the new holder would delete
anyway, and `tb_delete_tenant` checks again that nothing is left.

`audit_events` and `tenant_deletions` are retained (ADR 0034 decision 3 and "Deletion records'
retention"). Rows of `webhook_deliveries` with no `tenant_id` are left to retention and are
reported once, at the end, as skipped with the reason `retention_only`.
"""

from collections.abc import Mapping
from typing import Any

from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.deletion.model import LeaseLost, StepContext, StepOutcome
from trustbridge.tenancy.context import tenant_context

DEFAULT_BATCH_SIZE = 500
"""Rows per pass when the composition root does not choose."""

PLAN: tuple[tuple[str, str], ...] = (
    ("credential_operations", "adapter_operations"),
    ("adapter_credentials", "access_grants"),
    ("adapter_resources", "adapter_resources"),
    ("scope_leases", "scope_leases"),
    ("tenant_repositories", "tenant_repositories"),
    ("webhook_deliveries", "webhook_bodies"),
)
"""`(table, category)` in deletion order. The category is the name in `counts`: it avoids the admin
view's secret words (`has_secret_word`), which the table names `credential_*` would not."""

UNRESOLVED_CATEGORY = "webhook_bodies_unresolved"
RETENTION_ONLY = "retention_only"

_GUARD = text(
    "SELECT 1 FROM tenant_deletions WHERE id = :id AND holder = :holder AND fence = :fence "
    "AND state = 'database' AND expires_at > clock_timestamp()"
)
_UNRESOLVED = text(
    "SELECT count(*) FROM webhook_deliveries "
    "WHERE host = :host AND tenant_id IS NULL AND body IS NOT NULL"
)
_CLEAR_BODIES = text(
    "WITH cleared AS (UPDATE webhook_deliveries SET body = NULL, "
    "headers = CAST('{}' AS jsonb), last_error = NULL, status = 'deleted' "
    "WHERE (host, delivery_id) IN (SELECT host, delivery_id FROM webhook_deliveries "
    "WHERE tenant_id = :tenant AND status <> 'deleted' LIMIT :limit) RETURNING 1) "
    "SELECT count(*) FROM cleared"
)


def _delete_batch(table: str) -> TextClause:
    """One batch delete; `table` is always one of `PLAN`'s constants."""
    return text(
        f"WITH gone AS (DELETE FROM {table} WHERE ctid IN "  # noqa: S608 -- constants only
        f"(SELECT ctid FROM {table} WHERE tenant_id = :tenant LIMIT :limit) RETURNING 1) "
        "SELECT count(*) FROM gone"
    )


_STATEMENTS = {
    table: _CLEAR_BODIES if table == "webhook_deliveries" else _delete_batch(table)
    for table, _ in PLAN
}


class SqlDatabaseStep:
    """The `DatabaseStep` over the app role's session factory."""

    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, batch_size: int = DEFAULT_BATCH_SIZE
    ) -> None:
        """Delete at most `batch_size` rows per pass."""
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self._factory = factory
        self._batch_size = batch_size

    async def run(self, ctx: StepContext) -> StepOutcome:
        """Clear one batch of the cursor's table, then move on when the table is empty."""
        record = ctx.record
        index, before = _position(record.step_cursor)
        table, category = PLAN[index]
        removed, unresolved = await self._pass(ctx, table, record.host)
        counts = {category: removed} if removed else {}
        if removed >= self._batch_size:
            return StepOutcome(
                counts=counts, cursor={"table": table, "removed": before + removed}, done=False
            )
        if index + 1 < len(PLAN):
            return StepOutcome(
                counts=counts, cursor={"table": PLAN[index + 1][0], "removed": 0}, done=False
            )
        skipped = (
            [{"category": UNRESOLVED_CATEGORY, "reason": RETENTION_ONLY, "count": unresolved}]
            if unresolved
            else []
        )
        return StepOutcome(counts=counts, skipped=skipped)

    async def _pass(self, ctx: StepContext, table: str, host: str) -> tuple[int, int]:
        """The rows removed from `table`, and (on the last table) the unresolved deliveries left."""
        record, lease = ctx.record, ctx.lease
        with tenant_context(record.tenant_id):
            async with self._factory() as session, session.begin():
                held = await session.execute(
                    _GUARD, {"id": lease.id, "holder": lease.holder, "fence": lease.fence}
                )
                if held.first() is None:
                    raise LeaseLost
                params: Mapping[str, Any] = {"tenant": record.tenant_id, "limit": self._batch_size}
                removed = int((await session.execute(_STATEMENTS[table], params)).scalar_one())
                last = table == PLAN[-1][0]
                unresolved = 0
                if last and removed < self._batch_size:
                    unresolved = int(
                        (await session.execute(_UNRESOLVED, {"host": host})).scalar_one()
                    )
        return removed, unresolved


def _position(cursor: Mapping[str, Any]) -> tuple[int, int]:
    """The table index and the rows removed from it so far; an unreadable cursor starts over.

    Starting over is safe: every pass deletes whatever the tenant still has.
    """
    names = [table for table, _ in PLAN]
    table = cursor.get("table")
    removed = cursor.get("removed")
    if table not in names:
        return 0, 0
    return names.index(table), removed if isinstance(removed, int) and removed >= 0 else 0
