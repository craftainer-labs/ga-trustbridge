"""Postgres-backed behaviour of the outbox, leases, schema gate and migrate Job.

These need the devcontainer stack's Postgres (and run in CI's stack); the unit tests next to
this file cover the same invariants against in-memory fakes. 0028 invariants 1, 9, 10, 12, 13,
18 and the migration/model drift guard.
"""

import asyncio
import importlib.util
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError

from alembic import command
from app.db import engine, session_factory
from tests.trustbridge.helpers import make_settings
from trustbridge.worker import migrate
from trustbridge.worker.deliveries import (
    DeliveryRecord,
    NewDelivery,
    PgDeliveryStore,
    is_relayable,
)
from trustbridge.worker.lease import (
    Lease,
    LeaseBusy,
    LeaseLost,
    LeaseManager,
    PgLeaseStore,
    guarded_execute,
)
from trustbridge.worker.models import Base, ScopeLease, SchemaCompat, Tenant, WebhookDelivery
from trustbridge.worker.schema import REQUIRED_SCHEMA_VERSION, SchemaGate
from trustbridge.worker.tenants import PgTenantResolver

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]
NG3_TABLES = {"webhook_deliveries", "scope_leases", "tenants", "schema_compat"}


@pytest.fixture(scope="module", autouse=True)
async def migrated() -> None:
    """Bring the shared database to head (the migrate Job's work, done by the test)."""
    await asyncio.to_thread(command.upgrade, Config(str(ROOT / "alembic.ini")), "head")


@pytest.fixture
async def ids() -> AsyncIterator[list[str]]:
    """Delivery ids created by a test; removed afterwards."""
    created: list[str] = []
    yield created
    async with session_factory() as session, session.begin():
        await session.execute(delete(WebhookDelivery).where(WebhookDelivery.delivery_id.in_(created)))


def _new(ids: list[str], **kw: object) -> NewDelivery:
    delivery_id = f"it-{uuid.uuid4()}"
    ids.append(delivery_id)
    return NewDelivery(
        delivery_id=delivery_id,
        host=str(kw.get("host", "github.com")),
        event="push",
        headers={"x-github-event": "push"},
        body=b'{"a": 1}',
        received_at=datetime.now(UTC),
    )


async def test_a_committed_row_is_visible_to_other_sessions_and_a_repeat_makes_no_second_row(
    ids: list[str],
) -> None:  # inv. 1
    store = PgDeliveryStore(session_factory)
    delivery = _new(ids)
    assert await store.insert(delivery) is True
    async with session_factory() as other:  # a different connection sees it: it is committed
        assert (await other.get(WebhookDelivery, delivery.delivery_id)) is not None
    assert await store.insert(delivery) is False
    async with session_factory() as session:
        rows = (
            await session.scalars(
                select(WebhookDelivery).where(WebhookDelivery.delivery_id == delivery.delivery_id)
            )
        ).all()
    assert len(rows) == 1
    got = await store.get(delivery.delivery_id)
    assert got is not None
    assert (got.body, got.status, got.attempts) == (b'{"a": 1}', "pending", 0)


async def test_concurrent_inserts_of_one_id_create_one_row(ids: list[str]) -> None:  # inv. 1
    store = PgDeliveryStore(session_factory)
    delivery = _new(ids)
    results = await asyncio.gather(*(store.insert(delivery) for _ in range(8)))
    assert sorted(results) == [False] * 7 + [True]


async def test_sql_relay_rules_match_the_in_memory_rules(ids: list[str]) -> None:
    store = PgDeliveryStore(session_factory)
    now = datetime.now(UTC)
    past, future = now - timedelta(minutes=5), now + timedelta(minutes=5)
    scenarios = {
        "fresh": {},
        "relayed": {"last_relayed_at": past},
        "delayed": {"next_attempt_at": future, "last_relayed_at": past},
        "due": {"next_attempt_at": past - timedelta(minutes=1), "last_relayed_at": past - timedelta(minutes=2)},
        "due-relayed-after": {"next_attempt_at": past - timedelta(minutes=2), "last_relayed_at": past},
        "processed": {"status": "processed"},
        "dead": {"status": "dead"},
    }
    host = f"it-{uuid.uuid4().hex[:8]}"
    records: dict[str, DeliveryRecord] = {}
    for name, extra in scenarios.items():
        delivery = _new(ids, host=host)
        await store.insert(delivery)
        values = {"status": "pending", **extra}
        async with session_factory() as session, session.begin():
            await session.execute(
                update(WebhookDelivery)
                .where(WebhookDelivery.delivery_id == delivery.delivery_id)
                .values(**values)
            )
        record = await store.get(delivery.delivery_id)
        assert record is not None
        records[delivery.delivery_id] = record
        scenarios[name]["id"] = delivery.delivery_id  # type: ignore[assignment]
    for mode, before in (("tick", None), ("resweep", past + timedelta(minutes=1)), ("host", None)):
        wanted = {
            i
            for i, r in records.items()
            if is_relayable(r, now, mode=mode, resweep_before=before, host=host)  # type: ignore[arg-type]
        }
        got = await store.list_relayable(now, mode=mode, resweep_before=before, host=host)  # type: ignore[arg-type]
        assert {i for i in got if i in records} == wanted, mode


# --- leases ---------------------------------------------------------------------------------


@pytest.fixture
async def lease_key() -> AsyncIterator[tuple[uuid.UUID, str]]:
    tenant, key = uuid.uuid4(), f"k-{uuid.uuid4()}"
    yield tenant, key
    async with session_factory() as session, session.begin():
        await session.execute(delete(ScopeLease).where(ScopeLease.tenant_id == tenant))


async def test_one_holder_at_a_time_across_concurrent_units_of_work(
    lease_key: tuple[uuid.UUID, str],
) -> None:  # inv. 9
    tenant, key = lease_key
    manager = LeaseManager(PgLeaseStore(session_factory), ttl_seconds=30)
    winners: list[int] = []

    async def attempt() -> None:
        try:
            async with manager.hold(tenant, "jfrog", key):
                winners.append(1)
                await asyncio.sleep(0.3)
        except LeaseBusy:
            return

    await asyncio.gather(*(attempt() for _ in range(6)))
    assert winners == [1]


async def test_fencing_is_checked_in_the_same_statement_as_the_write(
    lease_key: tuple[uuid.UUID, str], ids: list[str]
) -> None:  # inv. 10
    tenant, key = lease_key
    store = PgLeaseStore(session_factory)
    target = _new(ids)
    await PgDeliveryStore(session_factory).insert(target)
    bump = update(WebhookDelivery).where(WebhookDelivery.delivery_id == target.delivery_id).values(
        attempts=WebhookDelivery.attempts + 1
    )

    fence_a = await store.acquire(tenant, "jfrog", key, "holder-a", ttl=0.4)
    assert fence_a == 1
    lease_a = Lease(tenant, "jfrog", key, "holder-a", fence_a)
    async with session_factory() as session, session.begin():
        assert await guarded_execute(session, lease_a, bump) == 1  # valid: the write lands
    assert await store.renew(lease_a, 0.4)  # renewing keeps the fence
    await asyncio.sleep(0.6)  # expired, and nobody has taken it over yet
    async with session_factory() as session:
        with pytest.raises(LeaseLost):
            await guarded_execute(session, lease_a, bump)  # expiry alone blocks the write
        await session.rollback()
    assert not await store.renew(lease_a, 5)

    fence_b = await store.acquire(tenant, "jfrog", key, "holder-b", ttl=30)
    assert fence_b == 2  # takeover bumps the fence
    async with session_factory() as session:
        with pytest.raises(LeaseLost):
            await guarded_execute(session, lease_a, bump)  # the old holder cannot commit
        await session.rollback()
    lease_b = Lease(tenant, "jfrog", key, "holder-b", fence_b)
    async with session_factory() as session, session.begin():
        assert await guarded_execute(session, lease_b, bump) == 1
    await store.release(lease_a)  # stale release: no effect on holder b
    assert await store.renew(lease_b, 30)
    got = await PgDeliveryStore(session_factory).get(target.delivery_id)
    assert got is not None
    assert got.attempts == 2  # exactly the two valid writes


# --- schema gate and the runtime role ------------------------------------------------------


async def test_schema_compat_is_one_row_at_or_above_the_required_version() -> None:  # inv. 13
    async with session_factory() as session:
        rows = (await session.scalars(select(SchemaCompat.version))).all()
    assert len(rows) == 1
    assert rows[0] >= REQUIRED_SCHEMA_VERSION
    assert await SchemaGate(session_factory).refresh() is True


def _migration_module() -> object:
    path = next((ROOT / "alembic" / "versions").glob("c3a5e7d91f42_*.py"))
    spec = importlib.util.spec_from_file_location("ng3_migration", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_the_runtime_role_can_read_the_schema_table_and_pass_the_gate() -> None:  # inv. 12
    role = f"tb_rt_{uuid.uuid4().hex[:8]}"
    migration = _migration_module()

    def grant(sync_conn: object) -> None:
        context = MigrationContext.configure(sync_conn)  # type: ignore[arg-type]
        with Operations.context(context):
            migration.grant_runtime_access(role)  # type: ignore[attr-defined]

    async with engine.connect() as conn:
        try:
            await conn.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
        except DBAPIError:
            pytest.skip("the test database user cannot create roles")
        try:
            await conn.run_sync(grant)
            await conn.execute(text(f'SET LOCAL ROLE "{role}"'))
            version = (await conn.execute(select(SchemaCompat.version))).scalar_one()
            assert version >= REQUIRED_SCHEMA_VERSION  # the gate's own query, as that role
            with pytest.raises(DBAPIError):  # and nothing more than SELECT on it
                async with conn.begin_nested():
                    await conn.execute(update(SchemaCompat).values(version=0))
        finally:
            await conn.rollback()
    async with engine.connect() as conn:
        await conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        await conn.commit()


async def test_the_models_and_the_migration_do_not_drift() -> None:
    def diff(sync_conn: object) -> list[object]:
        context = MigrationContext.configure(
            sync_conn,  # type: ignore[arg-type]
            opts={
                "include_object": lambda obj, name, type_, reflected, compare_to: (
                    type_ != "table" or name in NG3_TABLES
                )
            },
        )
        return list(compare_metadata(context, Base.metadata))

    async with engine.connect() as conn:
        assert await conn.run_sync(diff) == []


# --- tenants and the migrate Job -----------------------------------------------------------


async def test_concurrent_first_sights_of_an_installation_agree_on_one_tenant() -> None:
    resolver = PgTenantResolver(session_factory)
    host, installation = f"it-{uuid.uuid4().hex[:8]}", 424242
    results = await asyncio.gather(*(resolver.resolve(host, installation) for _ in range(6)))
    assert len(set(results)) == 1
    async with session_factory() as session, session.begin():
        await session.execute(delete(Tenant).where(Tenant.host == host))


async def test_two_concurrent_migrate_runs_do_not_interleave_and_report_the_role() -> None:  # 18
    settings = make_settings(mode="dev", allow_mock_mode=False)
    spans: list[tuple[str, float]] = []

    def upgrade(name: str):  # type: ignore[no-untyped-def]  # noqa: ANN202
        def run() -> None:
            import time  # noqa: PLC0415

            spans.append((f"{name}-start", time.monotonic()))
            time.sleep(0.4)
            spans.append((f"{name}-end", time.monotonic()))

        return run

    codes = await asyncio.gather(
        migrate.run_upgrade(settings, upgrade=upgrade("a")),
        migrate.run_upgrade(settings, upgrade=upgrade("b")),
    )
    assert codes == [0, 0]
    labels = [label for label, _ in sorted(spans, key=lambda s: s[1])]
    assert labels in (["a-start", "a-end", "b-start", "b-end"], ["b-start", "b-end", "a-start", "a-end"])


async def test_the_migrate_job_refuses_the_wrong_role() -> None:  # inv. 18
    settings = make_settings(mode="dev", allow_mock_mode=False, migrate_expected_role="nobody-here")
    assert await migrate.run_upgrade(settings, upgrade=lambda: None) == 1
