"""A whole tenant deletion against a real Postgres, fake adapters and a fake GitHub (NG6h-7a).

Runs in CI's stack (integration). A scratch database with RLS enforced, as `trustbridge_app`: the
real record store, the real drain port (`SqlWritersPort`) and the real database step, with a fake
platform port and a fake GitHub port on the two sides that talk to other systems. One run takes a
tenant from the request through drain, platform, GitHub and database to `done` and the tombstone,
and a webhook delivery recorded after the database step cleared its rows (the late delivery) is
cleared by the retry that `tb_delete_tenant`'s emptiness backstop forces.
"""

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import text

from tests.trustbridge.deletion.test_database_integration import (
    DELIVERY,
    ENVIRONMENT_INSERT,
    ROWS,
    TENANT_TABLES,
    _remaining,
    _seed,
)
from tests.trustbridge.deletion.test_sql_integration import CONFIG
from tests.trustbridge.tenancy.test_deletion_db_integration import (
    NEW_FUNCTIONS,
    Tenant,
    _admin,
    _admin_rows,
    _admin_scalar,
    _request,
    _tenant,
)
from tests.trustbridge.tenancy.test_rls_integration import OWNER_ROLE, Db, _provision
from trustbridge.deletion.database import SqlDatabaseStep
from trustbridge.deletion.drain import DrainStep
from trustbridge.deletion.engine import DeletionEngine, EngineSteps
from trustbridge.deletion.github import GithubDeletionStep
from trustbridge.deletion.model import StepContext, StepOutcome
from trustbridge.deletion.platform import AdapterPlatformStep
from trustbridge.deletion.ports import GithubOutput, PlatformItem, TrackedEnvironment
from trustbridge.deletion.sql import SqlDeletionStore
from trustbridge.worker.drain_deletion import SqlWritersPort

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
async def db() -> AsyncIterator[Db]:
    """A scratch database of its own, with the functions owned by the owner role."""
    async for provisioned in _provision(enforce=True):
        async with provisioned.admin.begin() as conn:
            for function in NEW_FUNCTIONS:
                await conn.execute(text(f"ALTER FUNCTION {function} OWNER TO {OWNER_ROLE}"))
            await conn.execute(text(f"ALTER TABLE github_environments OWNER TO {OWNER_ROLE}"))
            await conn.execute(text("ALTER TABLE github_environments ENABLE ROW LEVEL SECURITY"))
            await conn.execute(text("ALTER TABLE github_environments FORCE ROW LEVEL SECURITY"))
            for seeded in (provisioned.a, provisioned.b):
                await conn.execute(text(ENVIRONMENT_INSERT), {"t": seeded, "k": "seed", "n": 1})
        yield provisioned


class _Credentials:
    """The credential core as far as the drain uses it: resuming settles the operation."""

    def __init__(self, db: Db) -> None:
        self._db = db
        self.resumed: list[UUID] = []

    async def resume_operation(self, operation_id: UUID) -> None:
        self.resumed.append(operation_id)
        await _admin(
            self._db,
            "UPDATE credential_operations SET status = 'finished' WHERE id = :i",
            {"i": operation_id},
        )


class _Platform:
    """The fake adapters' recorded state: one credential and one project."""

    def __init__(self) -> None:
        self.items = [
            PlatformItem("credential", "fake", "s1"),
            PlatformItem("project", "fake", "p"),
        ]
        self.removed: list[PlatformItem] = []

    async def pending(self, tenant_id: UUID) -> Sequence[PlatformItem]:
        return list(self.items)

    async def remove(self, tenant_id: UUID, deletion_id: UUID, item: PlatformItem) -> None:
        self.items.remove(item)
        self.removed.append(item)


class _Connections:
    async def names(self, tenant_id: UUID) -> Sequence[str]:
        return ["argocd"]


class _Github:
    """The fake GitHub: the installation exists, one secret is recorded."""

    def __init__(self) -> None:
        self.outputs_left = [GithubOutput("fake", "s1", "secret", "TOKEN", 10, None)]
        self.removed: list[GithubOutput] = []

    async def installation_exists(self, host: str, installation_id: int) -> bool:
        return True

    async def outputs(self, tenant_id: UUID) -> Sequence[GithubOutput]:
        return list(self.outputs_left)

    async def remove_output(self, tenant_id: UUID, output: GithubOutput) -> bool:
        self.outputs_left.remove(output)
        self.removed.append(output)
        return True

    async def environments(self, tenant_id: UUID) -> Sequence[TrackedEnvironment]:
        return []

    async def untracked_environments(self, tenant_id: UUID) -> int:
        return 0

    async def environment_created_at(
        self, tenant_id: UUID, environment: TrackedEnvironment
    ) -> datetime | None:
        return None

    async def delete_environment(self, tenant_id: UUID, environment: TrackedEnvironment) -> None:
        return None

    async def release_environment(self, tenant_id: UUID, environment: TrackedEnvironment) -> None:
        return None


class _LateDelivery:
    """The database step, plus a delivery that lands for the tenant after its last table."""

    def __init__(self, db: Db, tenant: Tenant, inner: SqlDatabaseStep) -> None:
        self._db, self._tenant, self._inner = db, tenant, inner
        self.injected = False

    async def run(self, ctx: StepContext) -> StepOutcome:
        outcome = await self._inner.run(ctx)
        if outcome.done and not self.injected:
            self.injected = True
            await _admin(
                self._db,
                DELIVERY,
                {
                    "h": self._tenant.host,
                    "d": "late",
                    "b": b"late payload",
                    "s": "pending",
                    "t": self._tenant.id,
                },
            )
        return outcome


async def test_a_tenant_goes_from_the_request_to_done_and_a_late_delivery_is_cleared(
    db: Db,
) -> None:
    """Request, drain, platform, GitHub, database, done, tombstone; the late delivery converges."""
    tenant = await _tenant(db)
    deletion: UUID = (await _request(db, tenant, platform=True)).deletion_id
    await _seed(db, tenant)
    credentials = _Credentials(db)
    platform, github = _Platform(), _Github()
    slept: list[float] = []

    async def settle(seconds: float) -> None:
        """The drain's wait: the held leases lapse meanwhile."""
        slept.append(seconds)
        await _admin(
            db,
            "UPDATE scope_leases SET expires_at = now() - interval '1 second' WHERE tenant_id = :t",
            {"t": tenant.id},
        )

    late = _LateDelivery(db, tenant, SqlDatabaseStep(db.app, batch_size=3))
    steps = EngineSteps(
        drain=DrainStep(
            SqlWritersPort(db.app, cast("Any", credentials)),
            poll_seconds=0,
            max_passes=5,
            sleep=settle,
        ),
        platform=AdapterPlatformStep(platform, _Connections()),
        github=GithubDeletionStep(github),
        database=late,
    )
    engine = DeletionEngine(SqlDeletionStore(db.app), steps, CONFIG)

    assert await engine.run_once() is True  # runs to the backstop's refusal of the late delivery
    assert late.injected
    assert (
        await _admin_scalar(db, "SELECT state FROM tenant_deletions WHERE id = :d", {"d": deletion})
        == "database"
    )
    await _admin(db, "UPDATE tenant_deletions SET retry_at = NULL WHERE id = :d", {"d": deletion})
    assert await engine.run_once() is True  # the retry clears the late row and finishes

    row = (
        await _admin_rows(
            db,
            "SELECT state, counts, skipped FROM tenant_deletions WHERE id = :d",
            {"d": deletion},
        )
    )[0]
    assert row.state == "done"
    counts = row.counts
    assert counts["platform_access_grants"] == 1
    assert counts["platform_projects"] == 1  # the request set delete_platform_resources
    assert counts["github_outputs_removed"] == 1
    assert counts["access_grants"] == ROWS
    # the pass that found the table empty was not saved (the backstop refused the finish), so the
    # count is a lower bound; the retry added the late delivery
    assert counts["webhook_bodies"] >= 1
    assert len(credentials.resumed) == ROWS  # every seeded open operation was settled first
    assert slept  # the held leases made the first drain pass wait
    assert [i.kind for i in platform.removed] == ["credential", "project"]
    assert [o.name for o in github.removed] == ["TOKEN"]
    assert await _remaining(db, tenant.id) == dict.fromkeys(TENANT_TABLES, 0)
    assert not await _admin_scalar(
        db, "SELECT count(*) FROM tenants WHERE id = :t", {"t": tenant.id}
    )
    tombstone = await _admin_scalar(
        db,
        "SELECT count(*) FROM tenant_tombstones WHERE host = :h AND installation_id = :i",
        {"h": tenant.host, "i": tenant.installation},
    )
    assert tombstone == 1
    bodies = await _admin_rows(
        db,
        "SELECT status, body FROM webhook_deliveries WHERE host = :h AND tenant_id = :t",
        {"h": tenant.host, "t": tenant.id},
    )
    assert bodies
    assert {(b.status, b.body) for b in bodies} == {("deleted", None)}
    actions = [
        e.action
        for e in await _admin_rows(
            db,
            "SELECT action FROM audit_events WHERE tenant_id = :t "
            "AND action LIKE 'tenant.deletion.%' ORDER BY seq",
            {"t": tenant.id},
        )
    ]
    assert actions[-1] == "tenant.deletion.completed"
    assert actions.count("tenant.deletion.step") >= 4  # requested->draining ... database->done
