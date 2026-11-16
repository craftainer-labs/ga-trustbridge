"""A tenant deletion from the admin route to the tombstone, with late webhook deliveries (NG6h-7c).

Runs in CI's stack (integration), never in the unit lane. A scratch database with RLS enforced,
as `trustbridge_app`. Unlike `test_flow_integration.py`, which drives the engine alone, this
starts at the admin routes (the grace window is 0) and runs the real worker over the real outbox
and gate while the deletion is in progress, as ADR 0034 ("After deletion") specifies:

- a delivery of a tenant being deleted is **deferred**, left pending, with its tenant id recorded;
  no handler runs, and the database step clears it (body NULL, status `deleted`);
- a delivery that lands after the database step's last pass is cleared by the retry that
  `tb_delete_tenant`'s emptiness backstop forces;
- a delivery after the deletion is **acknowledged and dropped** (the tombstone), unhandled.

The second test takes the skip-step path: a failing platform step is skipped, and a skipped
database step does not finish the deletion while rows remain.
"""

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text

from tests.trustbridge.admin.test_tenant_deletion_integration import SKIP_FUNCTION, _client
from tests.trustbridge.deletion.test_database_integration import (
    ENVIRONMENT_INSERT,
    ROWS,
    TENANT_TABLES,
    _remaining,
    _seed,
)
from tests.trustbridge.deletion.test_flow_integration import (
    _Connections,
    _Credentials,
    _Github,
    _Platform,
)
from tests.trustbridge.deletion.test_sql_integration import CONFIG
from tests.trustbridge.fakes import FakeHostChecker, FakeRedis
from tests.trustbridge.helpers import as_redis, host, make_monitor, make_settings
from tests.trustbridge.tenancy.test_deletion_db_integration import (
    NEW_FUNCTIONS,
    Tenant,
    _admin,
    _admin_rows,
    _admin_scalar,
    _tenant,
)
from tests.trustbridge.tenancy.test_rls_integration import OWNER_ROLE, Db, _provision
from trustbridge.deletion.database import SqlDatabaseStep
from trustbridge.deletion.drain import DrainStep
from trustbridge.deletion.engine import DeletionEngine, EngineSteps
from trustbridge.deletion.github import GithubDeletionStep
from trustbridge.deletion.model import StepContext, StepOutcome
from trustbridge.deletion.platform import AdapterPlatformStep
from trustbridge.deletion.ports import PlatformItem
from trustbridge.deletion.sql import SqlDeletionStore
from trustbridge.deletion.steps import Step
from trustbridge.github.handlers import EventContext, HandlerRegistry
from trustbridge.tenancy import install_tenant_hook
from trustbridge.tenancy.gate import SqlTenantGate
from trustbridge.worker.deliveries import NewDelivery, PgDeliveryStore
from trustbridge.worker.drain_deletion import SqlWritersPort
from trustbridge.worker.queue import StreamQueue
from trustbridge.worker.relay import Relay
from trustbridge.worker.worker import Worker, WorkerDeps

pytestmark = pytest.mark.integration

ORG = "acme-org"


@pytest.fixture(scope="module")
async def db() -> AsyncIterator[Db]:
    """A scratch database of its own, with the functions owned by the owner role."""
    async for provisioned in _provision(enforce=True):
        async with provisioned.admin.begin() as conn:
            for function in (*NEW_FUNCTIONS, SKIP_FUNCTION):
                await conn.execute(text(f"ALTER FUNCTION {function} OWNER TO {OWNER_ROLE}"))
            await conn.execute(text(f"ALTER TABLE github_environments OWNER TO {OWNER_ROLE}"))
            await conn.execute(text("ALTER TABLE github_environments ENABLE ROW LEVEL SECURITY"))
            await conn.execute(text("ALTER TABLE github_environments FORCE ROW LEVEL SECURITY"))
            for seeded in (provisioned.a, provisioned.b):
                await conn.execute(text(ENVIRONMENT_INSERT), {"t": seeded, "k": "seed", "n": 1})
        yield provisioned


@pytest.fixture(scope="module", autouse=True)
def _hook() -> None:
    install_tenant_hook()


class _Webhooks:
    """The real worker over the Postgres outbox and the real gate, with a recording handler."""

    def __init__(self, db: Db, host_name: str) -> None:
        settings = make_settings([host(host_name)])
        self.handled: list[str] = []
        registry = HandlerRegistry()
        registry.register("push", self._handle)
        self.store = PgDeliveryStore(db.app)
        queue = StreamQueue(as_redis(FakeRedis()))
        self.queue = queue
        self.relay = Relay(self.store, queue)
        self.hosts = make_monitor(settings.github_hosts, FakeHostChecker(), check_timeout=1)
        deps = WorkerDeps(
            store=self.store,
            queue=queue,
            relay=self.relay,
            hosts=self.hosts,
            registry=registry,
            gate=SqlTenantGate(db.app),
        )
        self.worker = Worker(settings, deps, "flow-1")

    async def _handle(self, ctx: EventContext) -> None:
        self.handled.append(ctx.delivery_id)

    async def start(self) -> None:
        await self.hosts.check_all()
        await self.queue.ensure_group()

    async def deliver(self, tenant: Tenant, delivery_id: str) -> None:
        """Store a `push` delivery of the tenant's installation, relay it and run the worker."""
        body = json.dumps(
            {"installation": {"id": tenant.installation}, "delivery": delivery_id}
        ).encode()
        received = datetime.now(UTC)
        inserted = await self.store.insert(
            NewDelivery(delivery_id, tenant.host, "push", {}, body, received)
        )
        assert inserted
        assert await self.relay.tick() >= 1
        assert await self.worker.run_once() >= 1


class _Then:
    """A step that runs `inner` and, the first time it reports done, runs `hook`."""

    def __init__(self, inner: Step, hook: Callable[[], Awaitable[None]]) -> None:
        self._inner, self._hook = inner, hook
        self.fired = False

    async def run(self, ctx: StepContext) -> StepOutcome:
        outcome = await self._inner.run(ctx)
        if outcome.done and not self.fired:
            self.fired = True
            await self._hook()
        return outcome


class _DownPlatform(_Platform):
    """The fake platform, unreachable until `down` is cleared."""

    def __init__(self) -> None:
        super().__init__()
        self.down = True

    async def pending(self, tenant_id: UUID) -> Sequence[PlatformItem]:
        if self.down:
            raise ConnectionError("platform unreachable")
        return await super().pending(tenant_id)


class _FailsOnce:
    """A step that raises on its first pass and then delegates."""

    def __init__(self, inner: Step) -> None:
        self._inner = inner
        self.failed = False

    async def run(self, ctx: StepContext) -> StepOutcome:
        if not self.failed:
            self.failed = True
            raise ConnectionError("database step interrupted")
        return await self._inner.run(ctx)


async def _delivery(db: Db, tenant: Tenant, delivery_id: str) -> Any:  # noqa: ANN401 -- a row
    rows = await _admin_rows(
        db,
        "SELECT status, body, headers, tenant_id, last_error FROM webhook_deliveries "
        "WHERE host = :h AND delivery_id = :d",
        {"h": tenant.host, "d": delivery_id},
    )
    assert len(rows) == 1
    return rows[0]


async def _trail(db: Db, tenant: Tenant) -> list[tuple[str, str]]:
    """The tenant's audit events in order, as `(action, outcome)`."""
    rows = await _admin_rows(
        db,
        "SELECT action, outcome FROM audit_events WHERE tenant_id = :t ORDER BY seq",
        {"t": tenant.id},
    )
    return [(r.action, r.outcome) for r in rows]


async def _request_deletion(
    admin: httpx.AsyncClient,
    tenant: Tenant,
    **extra: Any,  # noqa: ANN401 -- body fields
) -> dict[str, Any]:
    """Disable the tenant and ask for its deletion; the 202 body (the grace window is 0)."""
    disabled = await admin.post(f"/admin/v1/tenants/{tenant.id}/disable", json={"reason": "hold"})
    assert disabled.status_code == 200, disabled.text
    body = {
        "host": tenant.host,
        "installation_id": tenant.installation,
        "organization": ORG,
        "reason": "customer asked",
        **extra,
    }
    response = await admin.post(f"/admin/v1/tenants/{tenant.id}/deletion", json=body)
    assert response.status_code == 202, response.text
    record: dict[str, Any] = response.json()
    assert record["state"] == "requested"
    assert record["not_before"] is None  # DELETION_GRACE_SECONDS is 0 in the test
    return record


def _lapse(db: Db, tenant: Tenant) -> Callable[[float], Awaitable[None]]:
    """The drain's wait: the leases the seeded rows hold lapse meanwhile."""

    async def settle(seconds: float) -> None:
        await _admin(
            db,
            "UPDATE scope_leases SET expires_at = now() - interval '1 second' WHERE tenant_id = :t",
            {"t": tenant.id},
        )

    return settle


def _engine(db: Db, steps: EngineSteps) -> DeletionEngine:
    return DeletionEngine(SqlDeletionStore(db.app), steps, CONFIG)


async def _run(db: Db, engine: DeletionEngine) -> None:
    """One claim, with the previous failure's backoff cleared so it is runnable at once."""
    await _admin(db, "UPDATE tenant_deletions SET retry_at = NULL")
    assert await engine.run_once() is True


async def _assert_cleared(db: Db, tenant: Tenant, *delivery_ids: str) -> None:
    """Each delivery holds no payload and is `deleted`, with the tenant recorded on it."""
    for delivery_id in delivery_ids:
        row = await _delivery(db, tenant, delivery_id)
        assert (row.status, row.body, row.headers, row.last_error) == ("deleted", None, {}, None)
        assert row.tenant_id == tenant.id


async def _assert_gone(db: Db, tenant: Tenant) -> None:
    """The tenant row and every row it had are gone, and its installation is tombstoned."""
    assert not await _admin_scalar(
        db, "SELECT count(*) FROM tenants WHERE id = :t", {"t": tenant.id}
    )
    assert await _remaining(db, tenant.id) == dict.fromkeys(TENANT_TABLES, 0)
    tombstones = await _admin_scalar(
        db,
        "SELECT count(*) FROM tenant_tombstones WHERE host = :h AND installation_id = :i",
        {"h": tenant.host, "i": tenant.installation},
    )
    assert tombstones == 1


async def test_a_deletion_runs_from_the_route_to_the_tombstone_and_defers_then_drops_deliveries(
    db: Db,
) -> None:
    """Request to done and tombstone; late deliveries are deferred and cleared, then dropped."""
    tenant = await _tenant(db, disabled=False)
    await _seed(db, tenant)
    webhooks = _Webhooks(db, tenant.host)
    await webhooks.start()
    credentials, platform, github = _Credentials(db), _Platform(), _Github()

    async def mid_deletion() -> None:
        """After the GitHub step: a delivery arrives while the tenant is `deleting`."""
        await webhooks.deliver(tenant, "mid")
        row = await _delivery(db, tenant, "mid")
        assert (row.status, row.tenant_id) == ("pending", tenant.id)  # deferred, tenant recorded
        assert row.body is not None  # still held: only the database step clears it

    async def after_last_pass() -> None:
        """After the database step's last pass: one more, which `tb_delete_tenant` refuses."""
        await webhooks.deliver(tenant, "late")
        assert (await _delivery(db, tenant, "late")).status == "pending"

    database = _Then(SqlDatabaseStep(db.app, batch_size=3), after_last_pass)
    github_step = _Then(GithubDeletionStep(github), mid_deletion)
    steps = EngineSteps(
        drain=DrainStep(
            SqlWritersPort(db.app, cast("Any", credentials)),
            poll_seconds=0,
            max_passes=5,
            sleep=_lapse(db, tenant),
        ),
        platform=AdapterPlatformStep(platform, _Connections()),
        github=github_step,
        database=database,
    )
    engine = _engine(db, steps)

    async with _client(db, "trustbridge:admin") as admin, _client(db, "trustbridge:viewer") as view:
        record = await _request_deletion(admin, tenant, delete_platform_resources=True)
        # A deletion in progress cannot be undone by `enable`: 409 `conflict`, not a 500.
        refused = await admin.post(f"/admin/v1/tenants/{tenant.id}/enable")
        assert refused.status_code == 409, refused.text
        assert refused.json()["type"] == "tag:trustbridge,2026:problem/conflict"

        await _run(db, engine)  # to the backstop's refusal of the late delivery
        assert github_step.fired
        assert database.fired
        assert (await view.get(f"/admin/v1/tenant-deletions/{record['id']}")).json()[
            "state"
        ] == "database"
        assert await _admin_scalar(
            db, "SELECT count(*) FROM tenants WHERE id = :t", {"t": tenant.id}
        )
        await _run(db, engine)  # the retry clears the late row and finishes

        final = (await view.get(f"/admin/v1/tenant-deletions/{record['id']}")).json()
        assert final["state"] == "done"
        assert final["hint"]
        assert final["counts"]["github_outputs_removed"] == 1
        assert final["counts"]["access_grants"] == ROWS
        assert final["counts"]["platform_projects"] == 1

    await _assert_gone(db, tenant)
    assert [o.name for o in github.removed] == ["TOKEN"]

    # both deliveries held during the deletion were cleared, never handled
    await _assert_cleared(db, tenant, "mid", "late")
    assert webhooks.handled == []

    # a delivery after the deletion is acknowledged and dropped: processed, no handler, no tenant
    await webhooks.deliver(tenant, "after")
    assert (await _delivery(db, tenant, "after")).status == "processed"
    assert webhooks.handled == []
    assert not await _admin_scalar(
        db, "SELECT count(*) FROM tenants WHERE host = :h", {"h": tenant.host}
    )

    # the audit trail: the routes' events and the engine's, in order (no event for a retry)
    assert await _trail(db, tenant) == [
        ("tenant.disable", "success"),
        ("tenant.deletion.requested", "success"),
        ("tenant.enable", "failure"),
        ("tenant.deletion.step", "success"),  # requested -> draining
        ("tenant.deletion.step", "success"),  # draining -> platform
        ("tenant.deletion.step", "success"),  # platform -> github
        (
            "tenant.deletion.skipped",
            "success",
        ),  # the platform step: connection data is operator-owned
        ("tenant.deletion.step", "success"),  # github -> database
        ("tenant.deletion.skipped", "success"),  # the unresolved deliveries (retention only)
        ("tenant.deletion.step", "success"),  # database -> done
        ("tenant.deletion.completed", "success"),
    ]


async def test_a_skipped_platform_step_goes_on_and_a_skipped_database_step_still_needs_empty_tables(
    db: Db,
) -> None:
    """Skip-step end to end: acknowledged, audited, and `tb_delete_tenant` has the last word."""
    tenant = await _tenant(db, disabled=False)
    await _seed(db, tenant)
    platform, github = _DownPlatform(), _Github()
    database = _FailsOnce(SqlDatabaseStep(db.app, batch_size=3))
    steps = EngineSteps(
        drain=DrainStep(
            SqlWritersPort(db.app, cast("Any", _Credentials(db))),
            poll_seconds=0,
            max_passes=5,
            sleep=_lapse(db, tenant),
        ),
        platform=AdapterPlatformStep(platform, _Connections()),
        github=GithubDeletionStep(github),
        database=database,
    )
    engine = _engine(db, steps)

    async with _client(db, "trustbridge:admin") as admin:
        record = await _request_deletion(admin, tenant)
        base = f"/admin/v1/tenant-deletions/{record['id']}"

        await _run(db, engine)  # the platform is unreachable: the step fails and is retried later
        failing = (await admin.get(base)).json()
        assert (failing["state"], failing["attempts"]) == ("platform", 1)
        assert failing["last_error_code"] == "ConnectionError"

        skipped = await admin.post(f"{base}/skip-step", json={"reason": "platform decommissioned"})
        assert skipped.status_code == 200, skipped.text
        shown = skipped.json()
        assert (shown["state"], shown["attempts"], shown["last_error_code"]) == ("github", 0, None)
        (entry,) = shown["skipped"]
        assert (entry["step"], entry["acknowledged_by"]) == ("platform", "alice")

        await _run(db, engine)  # GitHub runs, then the database step is interrupted once
        interrupted = (await admin.get(base)).json()
        assert (interrupted["state"], interrupted["attempts"]) == ("database", 1)
        assert [o.name for o in github.removed] == ["TOKEN"]
        assert platform.removed == []  # the skipped step removed nothing

        again = await admin.post(f"{base}/skip-step", json={"reason": "tables are stuck"})
        assert again.status_code == 200, again.text
        assert (again.json()["state"], again.json()["attempts"]) == ("database", 0)
        assert len(again.json()["skipped"]) == 2

        await _run(db, engine)  # the batches no longer run; the finish is refused: rows remain
        stuck = (await admin.get(base)).json()
        assert stuck["state"] == "database"
        assert stuck["attempts"] == 1
        assert await _remaining(db, tenant.id) == dict.fromkeys(TENANT_TABLES, ROWS)
        assert await _admin_scalar(
            db, "SELECT count(*) FROM tenants WHERE id = :t", {"t": tenant.id}
        )
        assert not await _admin_scalar(
            db, "SELECT count(*) FROM tenant_tombstones WHERE host = :h", {"h": tenant.host}
        )

        failed = await admin.post(f"{base}/fail", json={"reason": "giving up"})
        assert failed.status_code == 200, failed.text
        assert failed.json()["state"] == "failed"

    assert await _trail(db, tenant) == [
        ("tenant.disable", "success"),
        ("tenant.deletion.requested", "success"),
        ("tenant.deletion.step", "success"),  # requested -> draining
        ("tenant.deletion.step", "success"),  # draining -> platform
        ("tenant.deletion.step_skipped", "success"),  # platform -> github
        ("tenant.deletion.step", "success"),  # github -> database
        ("tenant.deletion.step_skipped", "success"),  # database, stays
        ("tenant.deletion.operator_failed", "success"),
    ]
