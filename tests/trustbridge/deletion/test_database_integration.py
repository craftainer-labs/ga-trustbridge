"""`SqlDatabaseStep` against a real Postgres (NG6h-4b, ADR 0034 step 5).

Runs in CI's stack (integration), never in the unit lane. A scratch database of its own, with RLS
enforced, as `trustbridge_app`: the step deletes through the tenant policies with no definer
function, so these tests are what prove the app role's grants and `tenant_isolation` policies are
enough. A tenant is provisioned with rows in every inventoried table and the step (and the engine
around it) runs in several small batches.
"""

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from tests.trustbridge.deletion.fakes import ScriptedStep, record
from tests.trustbridge.deletion.test_sql_integration import CONFIG, _Quiet
from tests.trustbridge.tenancy.test_deletion_db_integration import (
    FENCE,
    HOLDER,
    NEW_FUNCTIONS,
    Tenant,
    _admin,
    _admin_rows,
    _admin_scalar,
    _request,
    _set_state,
    _tenant,
)
from tests.trustbridge.tenancy.test_rls_integration import INSERTS, OWNER_ROLE, Db, _provision
from trustbridge.deletion.database import PLAN, SqlDatabaseStep
from trustbridge.deletion.drain import DrainStep
from trustbridge.deletion.engine import DeletionEngine, EngineSteps
from trustbridge.deletion.model import Lease, LeaseLost, StepContext
from trustbridge.deletion.sql import SqlDeletionStore

pytestmark = pytest.mark.integration

ROWS = 5
BATCH = 2
TENANT_TABLES = tuple(table for table, _ in PLAN if table != "webhook_deliveries")
DELIVERY = (
    "INSERT INTO webhook_deliveries "
    "(host, delivery_id, event, headers, body, status, tenant_id, last_error) "
    "VALUES (:h, :d, 'push', CAST('{\"x-forwarded-for\": \"203.0.113.9\"}' AS jsonb), "
    "CAST(:b AS bytea), :s, :t, 'handler failed for acme/widgets')"
)


@pytest.fixture(scope="module")
async def db() -> AsyncIterator[Db]:
    """A scratch database of its own, with the functions owned by the owner role."""
    async for provisioned in _provision(enforce=True):
        async with provisioned.admin.begin() as conn:
            for function in NEW_FUNCTIONS:
                await conn.execute(text(f"ALTER FUNCTION {function} OWNER TO {OWNER_ROLE}"))
        yield provisioned


async def _seed(db: Db, tenant: Tenant, rows: int = ROWS) -> None:
    """`rows` rows in each tenant-keyed table; deliveries: `rows` recorded, two with no tenant."""
    for table in TENANT_TABLES:
        for number in range(rows):
            await _admin(db, INSERTS[table], {"t": tenant.id, "k": f"k{number}", "n": 10 + number})
    statuses = ("pending", "processed", "dead")
    for number in range(rows):
        await _delivery(db, tenant.host, f"d{number}", statuses[number % 3], tenant.id)
    for number in range(2):
        await _delivery(db, tenant.host, f"unresolved{number}", "processed", None)
    await _delivery(db, f"other-{tenant.host}", "elsewhere", "processed", None)


async def _delivery(db: Db, host: str, delivery: str, status: str, tenant: UUID | None) -> None:
    params = {"h": host, "d": delivery, "b": b"payload", "s": status, "t": tenant}
    await _admin(db, DELIVERY, params)


async def _count(db: Db, table: str, tenant: UUID) -> int:
    sql = f"SELECT count(*) FROM {table} WHERE tenant_id = :t"  # noqa: S608 -- a constant name
    return int(await _admin_scalar(db, sql, {"t": tenant}))


def _json(value: Any) -> Any:  # noqa: ANN401 -- jsonb arrives decoded or as text
    return json.loads(value) if isinstance(value, str) else value


async def _remaining(db: Db, tenant: UUID) -> dict[str, int]:
    return {table: await _count(db, table, tenant) for table in TENANT_TABLES}


def _step(db: Db, batch: int = BATCH) -> SqlDatabaseStep:
    return SqlDatabaseStep(db.app, batch_size=batch)


def _ctx(tenant: Tenant, deletion: UUID, cursor: dict[str, Any], fence: int = FENCE) -> StepContext:
    rec = record(
        id=deletion,
        tenant_id=tenant.id,
        host=tenant.host,
        installation_id=tenant.installation,
        state="database",
        step_cursor=cursor,
        fence=fence,
    )

    async def renew() -> None:
        return None

    return StepContext(rec, Lease(deletion, HOLDER, fence), renew)


async def _in_database_step(db: Db) -> tuple[Tenant, UUID]:
    tenant = await _tenant(db)
    deletion: UUID = (await _request(db, tenant)).deletion_id
    await _set_state(db, deletion, "database")
    return tenant, deletion


async def _close(db: Db, deletion: UUID) -> None:
    await _admin(
        db,
        "UPDATE tenant_deletions SET state = 'cancelled', holder = NULL, expires_at = NULL "
        "WHERE id = :d",
        {"d": deletion},
    )


async def test_the_step_clears_every_inventoried_table_in_batches_and_resumes_from_its_cursor(
    db: Db,
) -> None:
    """ADR 0034 inv. 6, 7: only the tenants row remains; each pass is one bounded batch."""
    tenant, deletion = await _in_database_step(db)
    await _seed(db, tenant)
    totals: dict[str, int] = {}
    cursor: dict[str, Any] = {}
    passes = 0
    while True:
        # A new step object each pass: nothing is kept in memory between passes.
        outcome = await _step(db).run(_ctx(tenant, deletion, cursor))
        passes += 1
        assert all(0 < value <= BATCH for value in outcome.counts.values())
        for category, value in outcome.counts.items():
            totals[category] = totals.get(category, 0) + value
        if outcome.done:
            break
        assert outcome.cursor is not None
        cursor = dict(outcome.cursor)
        assert passes < 30
    assert passes == 3 * len(PLAN)  # three batches (2, 2, 1) per table; the last one is final
    assert totals == {
        "adapter_operations": ROWS,
        "access_grants": ROWS,
        "adapter_resources": ROWS,
        "scope_leases": ROWS,
        "tenant_repositories": ROWS,
        "webhook_bodies": ROWS,
    }
    assert await _remaining(db, tenant.id) == dict.fromkeys(TENANT_TABLES, 0)
    assert await _admin_scalar(db, "SELECT count(*) FROM tenants WHERE id = :t", {"t": tenant.id})
    assert outcome.skipped == [
        {"category": "webhook_bodies_unresolved", "reason": "retention_only", "count": 2}
    ]
    rows = await _admin_rows(
        db,
        "SELECT status, body IS NULL AS cleared, headers = CAST('{}' AS jsonb) AS bare, "
        "last_error IS NULL AS no_error FROM webhook_deliveries WHERE tenant_id = :t",
        {"t": tenant.id},
    )
    assert len(rows) == ROWS  # the dedupe rows stay
    assert {(row.status, row.cleared, row.bare, row.no_error) for row in rows} == {
        ("deleted", True, True, True)
    }
    # Deliveries with no tenant, on this host or another, are untouched.
    kept = await _admin_rows(
        db,
        "SELECT body IS NOT NULL AS has_body, status FROM webhook_deliveries "
        "WHERE tenant_id IS NULL AND host IN (:h, :o)",
        {"h": tenant.host, "o": f"other-{tenant.host}"},
    )
    assert len(kept) == 3
    assert {(row.has_body, row.status) for row in kept} == {(True, "processed")}
    # A second run finds nothing: every pass is idempotent.
    again = await _step(db).run(_ctx(tenant, deletion, {"table": PLAN[-1][0]}))
    assert again.done is True
    assert again.counts == {}
    await _close(db, deletion)


async def test_other_tenants_rows_survive(db: Db) -> None:
    """ADR 0034 inv. 13: the tenants of the seed keep one row in each keyed table."""
    tenant, deletion = await _in_database_step(db)
    await _seed(db, tenant, rows=1)
    await _delivery(db, tenant.host, "bs", "processed", db.b)
    cursor: dict[str, Any] = {}
    for _ in range(3 * len(PLAN)):
        outcome = await _step(db).run(_ctx(tenant, deletion, cursor))
        if outcome.done:
            break
        cursor = dict(outcome.cursor or {})
    assert outcome.done
    for owner in (db.a, db.b):
        assert await _remaining(db, owner) == dict.fromkeys(TENANT_TABLES, 1)
    assert await _admin_scalar(
        db,
        "SELECT count(*) FROM webhook_deliveries WHERE tenant_id = :t AND body IS NOT NULL",
        {"t": db.b},
    )
    await _close(db, deletion)


@pytest.mark.parametrize("fault", ["fence", "expired", "state"])
async def test_a_holder_without_the_live_lease_deletes_nothing(db: Db, fault: str) -> None:
    """A stale fence, an expired lease or a deletion no longer in `database` stops the pass."""
    tenant, deletion = await _in_database_step(db)
    await _seed(db, tenant, rows=1)
    before = await _remaining(db, tenant.id)
    if fault == "expired":
        await _admin(
            db,
            "UPDATE tenant_deletions SET expires_at = now() - interval '1 second' WHERE id = :d",
            {"d": deletion},
        )
    if fault == "state":
        await _admin(
            db, "UPDATE tenant_deletions SET state = 'github' WHERE id = :d", {"d": deletion}
        )
    stale = FENCE + 1 if fault == "fence" else FENCE
    with pytest.raises(LeaseLost):
        await _step(db).run(_ctx(tenant, deletion, {}, fence=stale))
    assert await _remaining(db, tenant.id) == before
    assert all(value == 1 for value in before.values())
    await _close(db, deletion)


async def test_the_engine_runs_the_step_to_done_and_removes_the_tenant_row_last(db: Db) -> None:
    """Through `DeletionEngine`: counts, the retention skip and the audit trail survive."""
    tenant = await _tenant(db)
    deletion: UUID = (await _request(db, tenant)).deletion_id
    await _seed(db, tenant)
    steps = EngineSteps(
        drain=DrainStep(_Quiet(), poll_seconds=0, max_passes=3),
        platform=ScriptedStep(),
        github=ScriptedStep(),
        database=_step(db),
    )
    assert await DeletionEngine(SqlDeletionStore(db.app), steps, CONFIG).run_once() is True
    row = (
        await _admin_rows(
            db,
            "SELECT state, counts, skipped FROM tenant_deletions WHERE id = :d",
            {"d": deletion},
        )
    )[0]
    assert row.state == "done"
    assert _json(row.counts) == {
        "adapter_operations": ROWS,
        "access_grants": ROWS,
        "adapter_resources": ROWS,
        "scope_leases": ROWS,
        "tenant_repositories": ROWS,
        "webhook_bodies": ROWS,
    }
    assert _json(row.skipped) == [
        {"category": "webhook_bodies_unresolved", "reason": "retention_only", "count": 2}
    ]
    assert not await _admin_scalar(
        db, "SELECT count(*) FROM tenants WHERE id = :t", {"t": tenant.id}
    )
    assert await _remaining(db, tenant.id) == dict.fromkeys(TENANT_TABLES, 0)
    events = await _admin_rows(
        db,
        "SELECT action, after FROM audit_events WHERE tenant_id = :t "
        "AND action LIKE 'tenant.deletion.%' ORDER BY seq",
        {"t": tenant.id},
    )
    assert [e.action for e in events][-3:] == [
        "tenant.deletion.skipped",
        "tenant.deletion.step",
        "tenant.deletion.completed",
    ]
    assert (
        _json(events[-2].after)["counts"]["access_grants"] == ROWS
    )  # not hidden by the admin view


async def test_the_engine_resumes_the_step_across_runs_when_its_budget_ends(db: Db) -> None:
    """Each run ends after a few passes (the fake clock); the saved cursor lets the next go on."""
    tenant = await _tenant(db)
    deletion: UUID = (await _request(db, tenant)).deletion_id
    await _seed(db, tenant, rows=3)
    steps = EngineSteps(
        drain=DrainStep(_Quiet(), poll_seconds=0, max_passes=3),
        platform=ScriptedStep(),
        github=ScriptedStep(),
        database=_step(db),
    )
    # Every renew ticks the clock once and the budget is 0.15 * 60 = 9 ticks: enough for the
    # drain's three renews (the drain keeps no cursor) but not for the whole database step, so the
    # engine releases the lease mid-step and a later run resumes from the saved cursor.
    ticks = iter(range(10_000))
    engine = DeletionEngine(
        SqlDeletionStore(db.app),
        steps,
        replace(CONFIG, budget_fraction=0.15),
        clock=lambda: next(ticks) * 1.0,
    )
    states = []
    for _ in range(80):
        await engine.run_once()
        state = await _admin_scalar(
            db, "SELECT state FROM tenant_deletions WHERE id = :d", {"d": deletion}
        )
        states.append(state)
        if state == "done":
            break
    assert states[-1] == "done"
    assert len(states) > 1  # the budget really did end runs early
    assert await _remaining(db, tenant.id) == dict.fromkeys(TENANT_TABLES, 0)


async def test_every_table_with_a_tenant_id_is_cleared_retained_or_listed(db: Db) -> None:
    """A new tenant-keyed table must be added to `PLAN` (or consciously retained) or this fails."""
    rows = await _admin_rows(
        db,
        "SELECT table_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND column_name = 'tenant_id'",
        {},
    )
    retained = {"audit_events", "tenant_deletions"}  # ADR 0034 decision 3, record retention
    assert {row.table_name for row in rows} == {table for table, _ in PLAN} | retained
