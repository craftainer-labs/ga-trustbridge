"""The deletion engine and `SqlDeletionStore` against a real Postgres (NG6h-4a-2, ADR 0034).

Runs in CI's stack (integration), never in the unit lane. A scratch database of its own, with RLS
enforced, as `trustbridge_app` (the production role). Step slots are doubles; the SQL is real:
the definer functions of migration `5d7a2c8e1b94`, `tb_delete_tenant`'s fence and the audit insert
under the tenant context.
"""

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from sqlalchemy import text

from tests.trustbridge.deletion.fakes import ScriptedStep
from tests.trustbridge.tenancy.test_deletion_db_integration import (
    NEW_FUNCTIONS,
    _admin,
    _admin_rows,
    _admin_scalar,
    _request,
    _tenant,
)
from tests.trustbridge.tenancy.test_rls_integration import OWNER_ROLE, Db, _provision
from trustbridge.deletion.drain import DrainStep
from trustbridge.deletion.engine import DeletionEngine, EngineConfig, EngineSteps
from trustbridge.deletion.model import NonRetryableError, StepOutcome
from trustbridge.deletion.sql import SqlDeletionStore

pytestmark = pytest.mark.integration

CONFIG = EngineConfig("engine-test", 60, 1, 4)


class _Quiet:
    async def quiesce(self, tenant_id: UUID) -> int:
        return 0


@pytest.fixture(scope="module")
async def db() -> AsyncIterator[Db]:
    """A scratch database of its own, with the functions owned by the owner role."""
    async for provisioned in _provision(enforce=True):
        async with provisioned.admin.begin() as conn:
            for function in NEW_FUNCTIONS:
                await conn.execute(text(f"ALTER FUNCTION {function} OWNER TO {OWNER_ROLE}"))
        yield provisioned


def _engine(db: Db, **overrides: ScriptedStep) -> DeletionEngine:
    steps = EngineSteps(
        drain=DrainStep(_Quiet(), poll_seconds=0, max_passes=3),
        platform=overrides.get("platform", ScriptedStep()),
        github=overrides.get("github", ScriptedStep()),
        database=overrides.get(
            "database", ScriptedStep(StepOutcome(counts={"tenant_repositories": 0}))
        ),
    )
    return DeletionEngine(SqlDeletionStore(db.app), steps, CONFIG)


async def _only_open(db: Db) -> None:
    await _admin(
        db,
        "UPDATE tenant_deletions SET state = 'cancelled', holder = NULL, expires_at = NULL "
        "WHERE state NOT IN ('done', 'failed', 'cancelled')",
    )


async def test_a_deletion_runs_to_done_and_completed_inserts_after_the_tenant_row_is_gone(
    db: Db,
) -> None:
    """ADR 0034 inv. 6, 12: the tenant row is removed last, in the transaction of `completed`."""
    await _only_open(db)
    tenant = await _tenant(db)
    deletion = (await _request(db, tenant)).deletion_id
    assert await _engine(db).run_once() is True
    row = (
        await _admin_rows(
            db, "SELECT state, holder FROM tenant_deletions WHERE id = :d", {"d": deletion}
        )
    )[0]
    assert (row.state, row.holder) == ("done", None)
    assert (
        await _admin_scalar(db, "SELECT count(*) FROM tenants WHERE id = :t", {"t": tenant.id}) == 0
    )
    actions = [
        r.action
        for r in await _admin_rows(
            db,
            "SELECT action FROM audit_events WHERE tenant_id = :t "
            "AND action LIKE 'tenant.deletion.%' ORDER BY seq",
            {"t": tenant.id},
        )
    ]
    assert actions == ["tenant.deletion.step"] * 4 + ["tenant.deletion.completed"]


async def test_a_non_retryable_error_writes_failed_and_its_event_together(db: Db) -> None:
    """`failed` is terminal and the event commits with it."""
    await _only_open(db)
    tenant = await _tenant(db)
    deletion = (await _request(db, tenant)).deletion_id
    engine = _engine(db, platform=ScriptedStep(NonRetryableError("bad_config", "no adapter")))
    assert await engine.run_once() is True
    row = (
        await _admin_rows(
            db, "SELECT state, last_error FROM tenant_deletions WHERE id = :d", {"d": deletion}
        )
    )[0]
    assert (row.state, row.last_error) == ("failed", "bad_config: no adapter")
    assert (
        await _admin_scalar(
            db,
            "SELECT count(*) FROM audit_events WHERE tenant_id = :t "
            "AND action = 'tenant.deletion.failed'",
            {"t": tenant.id},
        )
        == 1
    )


async def test_a_transient_error_backs_off_and_counts_the_attempt(db: Db) -> None:
    """The record keeps `last_error`, `attempts` and a `retry_at` in the future; no lease."""
    await _only_open(db)
    tenant = await _tenant(db)
    deletion = (await _request(db, tenant)).deletion_id
    assert await _engine(db, github=ScriptedStep(RuntimeError("down"))).run_once() is True
    row = (
        await _admin_rows(
            db,
            "SELECT state, attempts, holder, last_error, retry_at > now() AS later "
            "FROM tenant_deletions WHERE id = :d",
            {"d": deletion},
        )
    )[0]
    assert (row.state, row.attempts, row.holder, row.later) == ("github", 1, None, True)
    assert row.last_error.startswith("RuntimeError: down")
