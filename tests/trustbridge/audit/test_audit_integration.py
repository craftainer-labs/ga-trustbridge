"""The audit log against a real Postgres: append-only layers, purge, scope, cursor, export.

ADR 0032 invariants 1, 2, 3, 3a, 4, 11 (storage half), 12 (ordering, resume, lag), 13 (shape),
15 and 16 (migration applies on the real chain). Runs in CI's stack (integration), never in the
unit lane. Each fixture provisions a throwaway database, migrates it to head with the real
Alembic chain (as the test database's own user, then hands `audit_events` and the definer
functions to `trustbridge_owner`, as the migrate Job's role would own them), and connects as
`trustbridge_app`, which is neither the owner nor a superuser. Tests use random tenant ids (no
foreign key) so they never see each other's rows.
"""

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import URL, Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.config import Settings
from app.db import build_engine
from trustbridge.adapters.interface import CredentialSlot, CredentialState
from trustbridge.adapters.store import (
    Operation,
    OperationKind,
    OperationStatus,
    ScopeId,
    StoredCredential,
)
from trustbridge.audit import (
    Action,
    ActorType,
    AuditDenials,
    AuditEvent,
    AuditQuery,
    AuditWriter,
    Outcome,
    SystemActor,
    TenantSyncAudit,
    append,
    export_ndjson,
    list_events,
    purge_batch,
    purge_expired,
)
from trustbridge.audit.credentials import CredentialAuditObserver
from trustbridge.audit.denial import Denial
from trustbridge.tenancy import install_tenant_hook, tenant_session

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
APP_ROLE = "trustbridge_app"
OWNER_ROLE = "trustbridge_owner"
OTHER_ROLE = "trustbridge_audit_other"  # a role with no grants at all
PASSWORD = "tb-audit-test"  # noqa: S105 -- a throwaway test role in a throwaway database
RLS_ERROR = "row-level security"
FUNCTIONS = (
    "tb_audit_purge(timestamptz, integer, integer)",
    "tb_audit_list(uuid, bigint, bigint, timestamptz, timestamptz, text, text, text, text, text,"
    " double precision, boolean, integer)",
)
COLUMNS = [
    "id",
    "seq",
    "occurred_at",
    "recorded_at",
    "tenant_id",
    "actor_type",
    "actor_sub",
    "actor_iss",
    "on_behalf_of",
    "source_ip",
    "action",
    "resource_type",
    "resource_id",
    "outcome",
    "before",
    "after",
    "request_id",
    "trace_id",
    "schema_version",
]
ROLES_SQL = """
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trustbridge_app') THEN
        CREATE ROLE trustbridge_app LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trustbridge_owner') THEN
        CREATE ROLE trustbridge_owner LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trustbridge_audit_other') THEN
        CREATE ROLE trustbridge_audit_other LOGIN;
    END IF;
END $$
"""


@dataclass(slots=True)
class Db:
    """A provisioned scratch database and the engines over it."""

    name: str
    admin: AsyncEngine
    app: async_sessionmaker[AsyncSession]
    owner: async_sessionmaker[AsyncSession]
    other: async_sessionmaker[AsyncSession]


def _engine(database: str, user: str) -> AsyncEngine:
    """The application's own engine builder, pointed at the scratch database as `user`."""
    return build_engine(
        Settings(postgres_user=user, postgres_password=PASSWORD, postgres_db=database)
    )


async def _provision() -> AsyncIterator[Db]:
    """Create and migrate a scratch database, re-own the audit objects; drop it afterwards."""
    s = Settings()
    name = f"tb_audit_{uuid4().hex[:10]}"

    def url(user: str, password: str, database: str) -> URL:
        return URL.create(
            "postgresql+asyncpg",
            username=user,
            password=password,
            host=s.postgres_host,
            port=s.postgres_port,
            database=database,
        )

    server = create_async_engine(
        url(s.postgres_user, s.postgres_password, s.postgres_db),
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    try:
        async with server.connect() as conn:
            await conn.execute(text(ROLES_SQL))
            for role in (APP_ROLE, OWNER_ROLE, OTHER_ROLE):
                await conn.execute(
                    text(
                        f"ALTER ROLE {role} LOGIN PASSWORD '{PASSWORD}' "
                        "NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB"
                    )
                )
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    except DBAPIError:
        await server.dispose()
        pytest.skip("the test database user cannot create roles and databases")
    proc = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, "POSTGRES_DB": name},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    admin = create_async_engine(url(s.postgres_user, s.postgres_password, name), poolclass=NullPool)
    engines = [
        admin,
        _engine(name, APP_ROLE),
        _engine(name, OWNER_ROLE),
        _engine(name, OTHER_ROLE),
    ]
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f"ALTER TABLE audit_events OWNER TO {OWNER_ROLE}"))
            for function in FUNCTIONS:
                await conn.execute(text(f"ALTER FUNCTION {function} OWNER TO {OWNER_ROLE}"))
        yield Db(
            name,
            admin,
            *(async_sessionmaker(engine, expire_on_commit=False) for engine in engines[1:]),
        )
    finally:
        for engine in engines:
            await engine.dispose()
        async with server.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await server.dispose()


@pytest.fixture(scope="module", autouse=True)
def _hook() -> None:
    """The process installs the hook once, as `build_core` and `main.install` do."""
    install_tenant_hook()


@pytest.fixture(scope="module")
async def db() -> AsyncIterator[Db]:
    """A migrated database shared by the tests that only add rows under their own tenant."""
    async for item in _provision():
        yield item


@pytest.fixture
async def fresh_db() -> AsyncIterator[Db]:
    """A database of its own, for tests that purge or read instance-wide."""
    async for item in _provision():
        yield item


def _event(tenant: UUID | None, **overrides: Any) -> AuditEvent:  # noqa: ANN401
    values: dict[str, Any] = {
        "actor_type": ActorType.USER,
        "actor_sub": "sub-1",
        "actor_iss": "https://idp.example",
        "action": "tenant.disable",
        "resource_type": "tenant",
        "outcome": Outcome.SUCCESS,
        "tenant_id": tenant,
    }
    values.update(overrides)
    return AuditEvent(**values)


async def _write(db: Db, tenant: UUID | None, **overrides: Any) -> UUID:  # noqa: ANN401
    """Append one event as the app role in its own committed transaction."""
    if tenant is None:
        async with db.app() as session, session.begin():
            return await append(session, _event(tenant, **overrides))
    async with tenant_session(db.app, tenant) as session, session.begin():
        return await append(session, _event(tenant, **overrides))


async def _scalar(
    factory: async_sessionmaker[AsyncSession],
    sql: str,
    **params: Any,  # noqa: ANN401
) -> Any:  # noqa: ANN401
    async with factory() as session:
        return (await session.execute(text(sql), params)).scalar_one()


async def _admin_scalar(db: Db, sql: str) -> Any:  # noqa: ANN401
    async with db.admin.connect() as conn:
        return (await conn.execute(text(sql))).scalar_one()


async def _run_owner(db: Db, sql: str, **params: Any) -> None:  # noqa: ANN401
    async with db.owner() as session, session.begin():
        await session.execute(text(sql), params)


async def _all(db: Db, tenant: UUID, **query: Any) -> list[int]:  # noqa: ANN401
    """The `seq`s of a tenant's events, walking the cursor to the end."""
    seqs: list[int] = []
    current = AuditQuery(**query)
    while True:
        async with tenant_session(db.app, tenant) as session:
            page = await list_events(session, current, tenant_id=tenant)
        seqs.extend(item.seq for item in page.items)
        if page.next_seq is None:
            return seqs
        key = "before_seq" if current.descending else "after_seq"
        current = AuditQuery(**{**query, key: page.next_seq})


# --- append-only (inv. 1-4) ----------------------------------------------------------------


async def test_the_app_role_can_insert_and_select_but_not_update_delete_or_truncate(
    db: Db,
) -> None:  # inv. 1
    """Grants: INSERT and SELECT of the context tenant's rows work; the rest is refused."""
    tenant = uuid4()
    event_id = await _write(db, tenant)
    async with tenant_session(db.app, tenant) as session:
        count = (await session.execute(text("SELECT count(*) FROM audit_events"))).scalar_one()
        assert count == 1
    privileges = {
        p: await _scalar(db.app, f"SELECT has_table_privilege('audit_events', '{p}')")
        for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
    }
    assert privileges == {
        "SELECT": True,
        "INSERT": True,
        "UPDATE": False,
        "DELETE": False,
        "TRUNCATE": False,
    }
    for statement in (
        "UPDATE audit_events SET outcome = 'failure'",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
    ):
        async with tenant_session(db.app, tenant) as session:
            with pytest.raises(DBAPIError, match="permission denied"):
                await session.execute(text(statement))
    assert await _scalar(db.owner, "SELECT count(*) FROM audit_events WHERE id = :i", i=event_id)


async def test_the_trigger_rejects_changes_by_anyone_but_the_owner_role(db: Db) -> None:  # inv. 2
    """With a grant mistake simulated, TRUNCATE hits the trigger, UPDATE/DELETE find no rows."""
    tenant = uuid4()
    await _write(db, tenant)
    async with db.admin.begin() as conn:
        await conn.execute(text(f"GRANT UPDATE, DELETE, TRUNCATE ON audit_events TO {APP_ROLE}"))
    try:
        async with tenant_session(db.app, tenant) as session:
            with pytest.raises(DBAPIError, match="append-only"):
                await session.execute(text("TRUNCATE audit_events"))
        # RLS has no UPDATE/DELETE policy for the app role: a third layer, nothing is touched.
        async with tenant_session(db.app, tenant) as session:
            update = await session.execute(text("UPDATE audit_events SET outcome = 'failure'"))
            delete = await session.execute(text("DELETE FROM audit_events"))
            assert (update.rowcount, delete.rowcount) == (0, 0)  # type: ignore[attr-defined]
            await session.rollback()
        # The trigger itself, past RLS and grants: the test database's own (superuser) role.
        async with db.admin.connect() as conn:
            for statement in (
                "UPDATE audit_events SET outcome = 'failure'",
                "DELETE FROM audit_events",
                "TRUNCATE audit_events",
            ):
                with pytest.raises(DBAPIError, match="append-only"):
                    await conn.execute(text(statement))
                await conn.rollback()
    finally:
        async with db.admin.begin() as conn:
            await conn.execute(
                text(f"REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM {APP_ROLE}")
            )
    # Only current_user = trustbridge_owner passes the trigger.
    await _run_owner(
        db, "UPDATE audit_events SET request_id = 'owner' WHERE tenant_id = :t", t=tenant
    )
    assert await _scalar(db.owner, "SELECT count(*) FROM audit_events WHERE request_id = 'owner'")


async def test_the_table_is_forced_row_level_security_and_the_functions_are_hardened(
    db: Db,
) -> None:
    """RLS enabled and forced from creation; definer functions pin search_path (ADR 0031/0032)."""
    flags = await _admin_scalar(
        db,
        "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class "
        "WHERE relname = 'audit_events'",
    )
    assert flags is True
    hardened = await _admin_scalar(
        db,
        "SELECT count(*) FROM pg_proc WHERE proname IN ('tb_audit_purge', 'tb_audit_list') "
        "AND prosecdef AND array_to_string(proconfig, ',') LIKE '%search_path=pg_catalog%'",
    )
    assert hardened == 2


async def test_a_null_tenant_event_is_inserted_without_a_context_and_foreign_ones_are_not(
    db: Db,
) -> None:  # inv. 3a
    """`authz.denied` has a null tenant and no context; the insert policy still bounds the rest."""
    marker = f"denied-{uuid4().hex[:8]}"
    await _write(db, None, action="authz.denied", resource_id=marker, outcome=Outcome.DENIED)
    assert await _scalar(
        db.owner, "SELECT count(*) FROM audit_events WHERE resource_id = :m", m=marker
    )
    a, b = uuid4(), uuid4()
    async with tenant_session(db.app, a) as session:
        with pytest.raises(DBAPIError, match=RLS_ERROR):
            await append(session, _event(b))
    async with db.app() as session:  # no context: only a null tenant is allowed
        with pytest.raises(DBAPIError, match=RLS_ERROR):
            await append(session, _event(a))


async def test_the_database_assigns_the_timestamps_and_seq(db: Db) -> None:  # inv. 4
    """A caller-supplied occurred_at / recorded_at is ignored; seq cannot be supplied."""
    tenant = uuid4()
    forged = datetime(2000, 1, 1, tzinfo=UTC)
    async with tenant_session(db.app, tenant) as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO audit_events (id, tenant_id, occurred_at, recorded_at, actor_type, "
                "actor_sub, action, resource_type, outcome) VALUES (:i, :t, :o, :o, 'system', "
                "'worker', 'tenant.sync', 'tenant', 'success')"
            ),
            {"i": uuid4(), "t": tenant, "o": forged},
        )
    stamped = await _scalar(
        db.owner,
        "SELECT occurred_at > now() - interval '1 hour' AND recorded_at > now() - interval '1 hour'"
        " FROM audit_events WHERE tenant_id = :t",
        t=tenant,
    )
    assert stamped is True
    async with tenant_session(db.app, tenant) as session:
        with pytest.raises(DBAPIError, match="seq"):
            await session.execute(
                text(
                    "INSERT INTO audit_events (id, tenant_id, seq, actor_type, actor_sub, action, "
                    "resource_type, outcome) VALUES (:i, :t, 1, 'system', 'worker', 'a.b', 'x', "
                    "'success')"
                ),
                {"i": uuid4(), "t": tenant},
            )


async def test_the_table_constraints_back_the_actor_and_outcome_rules(db: Db) -> None:
    """A user row without iss, a system row with one and an unknown outcome are refused."""
    base = (
        "INSERT INTO audit_events (id, actor_type, actor_sub, actor_iss, action, resource_type, "
        "outcome) VALUES (:i, :a, 'x', :iss, 'a.b', 'x', :o)"
    )
    for actor, iss, outcome in (
        ("user", None, "success"),
        ("system", "https://idp", "success"),
        ("system", None, "maybe"),
    ):
        async with db.owner() as session:
            with pytest.raises(DBAPIError, match="violates check constraint"):
                await session.execute(
                    text(base), {"i": uuid4(), "a": actor, "iss": iss, "o": outcome}
                )


# --- purge and retention (inv. 3, 15) -----------------------------------------------------


async def _seed_aged(db: Db, ages_days: list[int]) -> list[UUID]:
    """Append null-tenant events, then (as the owner) set their `occurred_at` back."""
    ids = [await _write(db, None, resource_id=f"age-{age}") for age in ages_days]
    for event_id, age in zip(ids, ages_days, strict=True):
        await _run_owner(
            db,
            "UPDATE audit_events SET occurred_at = now() - make_interval(days => :d) WHERE id = :i",
            d=age,
            i=event_id,
        )
    return ids


async def test_the_purge_function_is_the_only_delete_path(fresh_db: Db) -> None:  # inv. 3
    """Deletes only rows past the cut-off, refuses the floor, writes a summary, is not public."""
    db = fresh_db
    old, mid, new = await _seed_aged(db, [200, 50, 5])
    cutoff = datetime.now(UTC) - timedelta(days=100)
    async with db.app() as session, session.begin():
        assert await purge_batch(session, before=cutoff, min_days=90) == 1
    remaining = set(await _all_ids(db.owner))
    assert old not in remaining
    assert {mid, new} <= remaining
    summary = await _scalar(
        db.owner,
        "SELECT \"after\"->>'deleted' FROM audit_events WHERE action = 'audit.purge'",
    )
    assert summary == "1"
    async with db.app() as session:  # the cut-off is inside the floor: refused, nothing deleted
        with pytest.raises(DBAPIError, match="retention floor"):
            await purge_batch(session, before=datetime.now(UTC) - timedelta(days=30), min_days=90)
    async with db.other() as session:  # EXECUTE is revoked from PUBLIC
        with pytest.raises(DBAPIError, match="permission denied"):
            await purge_batch(session, before=cutoff, min_days=90)
    assert {mid, new} <= set(await _all_ids(db.owner))


async def _all_ids(factory: async_sessionmaker[AsyncSession]) -> list[UUID]:
    async with factory() as session:
        result = await session.execute(text("SELECT id FROM audit_events"))
        return [UUID(str(i)) for i in result.scalars()]


async def test_retention_zero_never_purges_and_otherwise_purges_in_batches(
    fresh_db: Db,
) -> None:  # inv. 15
    """0 days touches nothing; a real retention removes only older rows, batch by batch."""
    db = fresh_db
    await _seed_aged(db, [400, 399, 398, 397, 396, 150, 10])
    assert await purge_expired(db.app, retention_days=0, min_days=90) == 0
    assert len(await _all_ids(db.owner)) == 7
    # Five rows past 365 days, in batches of two: three transactions (2, 2, 1), a summary each.
    assert await purge_expired(db.app, retention_days=365, min_days=90, batch_size=2) == 5
    assert await _count(db, "resource_id = 'age-10'") == 1
    assert await _count(db, "action = 'audit.purge'") == 3
    # `now` is injectable: from 60 days ago, 100 days of retention reaches back 160 days, so the
    # 150-day-old row stays; with the real clock it goes.
    past = datetime.now(UTC) - timedelta(days=60)
    assert await purge_expired(db.app, retention_days=100, min_days=90, now=past) == 0
    assert await purge_expired(db.app, retention_days=100, min_days=90) == 1
    assert await _count(db, "resource_id IN ('age-10', 'age-150')") == 1


async def _count(db: Db, where: str) -> int:
    sql = f"SELECT count(*) FROM audit_events WHERE {where}"  # noqa: S608 -- literals in tests
    return int(await _scalar(db.owner, sql))


# --- scope, cursor and filters (inv. 11 storage half) -------------------------------------


async def test_a_tenant_reads_only_its_own_events_and_instance_scope_reads_all(
    fresh_db: Db,
) -> None:  # inv. 11
    """Tenant scope never returns another tenant's or a null-tenant event; instance scope does."""
    db = fresh_db
    a, b = uuid4(), uuid4()
    mine = await _write(db, a)
    theirs = await _write(db, b)
    nobody = await _write(db, None, action="authz.denied", outcome=Outcome.DENIED)
    async with tenant_session(db.app, a) as session:
        page = await list_events(session, AuditQuery(), tenant_id=a)
        assert [item.id for item in page.items] == [mine]
        with pytest.raises(ValueError, match="conflicts"):
            await list_events(session, AuditQuery(tenant_id=b), tenant_id=a)
        # Even if the explicit filter were dropped, RLS leaves a tenant only its own rows.
        rows = (await session.execute(text("SELECT id FROM audit_events"))).scalars().all()
        assert [UUID(str(i)) for i in rows] == [mine]
    async with db.app() as session:  # instance scope: no context needed (definer function)
        everything = await list_events(session, AuditQuery(), instance_scope=True)
        assert {item.id for item in everything.items} == {mine, theirs, nobody}
        only_b = await list_events(session, AuditQuery(tenant_id=b), instance_scope=True)
        assert [item.id for item in only_b.items] == [theirs]
        denied = next(item for item in everything.items if item.id == nobody)
        assert denied.tenant_id is None
        assert denied.outcome is Outcome.DENIED


async def test_the_cursor_is_ordered_resumable_and_filters_agree_across_scopes(db: Db) -> None:
    """Ascending and descending pages cover every event once; filters match in both scopes."""
    tenant = uuid4()
    ids = [
        await _write(
            db,
            tenant,
            action="credential.rotate" if n % 2 else "credential.issue",
            actor_sub=f"sub-{n % 3}",
            resource_id=f"res-{n}",
            outcome=Outcome.FAILURE if n == 4 else Outcome.SUCCESS,
            before={"n": n},
            after={"n": n + 1},
            source_ip="198.51.100.9",
            request_id=f"req-{n}",
            trace_id=f"trace-{n}",
        )
        for n in range(7)
    ]
    asc = await _all(db, tenant, limit=3)
    assert len(asc) == 7
    assert asc == sorted(asc)
    assert await _all(db, tenant, limit=3, descending=True) == asc[::-1]
    assert await _all(db, tenant, limit=1000) == asc
    async with tenant_session(db.app, tenant) as session:
        first = await list_events(session, AuditQuery(limit=2), tenant_id=tenant)
        assert first.next_seq == asc[1]
        assert [item.id for item in first.items] == ids[:2]
        resumed = await list_events(
            session, AuditQuery(limit=100, after_seq=first.next_seq), tenant_id=tenant
        )
        assert [item.seq for item in resumed.items] == asc[2:]
        assert resumed.next_seq is None
        filters: list[dict[str, Any]] = [
            {"action": "credential.rotate"},
            {"actor_sub": "sub-1"},
            {"resource_type": "tenant", "resource_id": "res-3"},
            {"outcome": Outcome.FAILURE},
            {"since": datetime.now(UTC) - timedelta(hours=1)},
            {"until": datetime.now(UTC) + timedelta(hours=1), "before_seq": asc[5]},
            {"since": datetime.now(UTC) + timedelta(hours=1)},
            {"lag_seconds": 0.0, "descending": True},
        ]
        for extra in filters:
            query = AuditQuery(**extra)
            own = await list_events(session, query, tenant_id=tenant)
            wide = await list_events(
                session, AuditQuery(**{**extra, "tenant_id": tenant}), instance_scope=True
            )
            assert [i.id for i in own.items] == [i.id for i in wide.items], extra
        rotated = await list_events(
            session, AuditQuery(action="credential.rotate"), tenant_id=tenant
        )
        assert len(rotated.items) == 3
        one = await list_events(session, AuditQuery(resource_id="res-4"), tenant_id=tenant)
        record = one.items[0]
        assert (record.before, record.after) == ({"n": 4}, {"n": 5})
        assert (record.source_ip, record.request_id, record.trace_id) == (
            "198.51.100.9",
            "req-4",
            "trace-4",
        )
        assert (record.outcome, record.actor_type, record.schema_version) == (
            Outcome.FAILURE,
            ActorType.USER,
            1,
        )


# --- export (inv. 12, 13) -----------------------------------------------------------------


async def _export(
    db: Db, tenant: UUID | None, lag: float, query: AuditQuery | None = None
) -> list[bytes]:
    """The export in pages of two, in the tenant's context or (None) instance-wide."""
    query = query or AuditQuery()
    if tenant is None:
        async with db.app() as session:
            stream = export_ndjson(
                session, query, instance_scope=True, lag_seconds=lag, page_size=2
            )
            return [line async for line in stream]
    async with tenant_session(db.app, tenant) as session:
        stream = export_ndjson(session, query, tenant_id=tenant, lag_seconds=lag, page_size=2)
        return [line async for line in stream]


async def test_the_export_is_ndjson_ordered_by_seq_and_resumable(db: Db) -> None:  # inv. 12
    """One JSON object per line, ascending, paged; resuming after a seq yields exactly the rest."""
    tenant = uuid4()
    for n in range(5):
        await _write(db, tenant, resource_id=f"res-{n}", after={"n": n})
    lines = await _export(db, tenant, 0.0)
    objects = [json.loads(line) for line in lines]
    assert all(line.endswith(b"\n") and line.count(b"\n") == 1 for line in lines)
    seqs = [o["seq"] for o in objects]
    assert len(seqs) == 5
    assert seqs == sorted(seqs)
    assert [o["resource_id"] for o in objects] == [f"res-{n}" for n in range(5)]
    assert list(objects[0]) == COLUMNS
    assert objects[0]["occurred_at"].endswith("Z")
    assert objects[0]["tenant_id"] == str(tenant)
    tail = await _export(db, tenant, 0.0, AuditQuery(after_seq=seqs[2]))
    assert [json.loads(line)["seq"] for line in tail] == seqs[3:]
    assert await _export(db, tenant, 3600.0) == []  # everything is inside the lag


@pytest.mark.parametrize("instance_scope", [False, True])
async def test_the_export_stops_before_the_first_event_inside_the_lag(
    fresh_db: Db, instance_scope: bool
) -> None:  # inv. 12 (lag)
    """A recent event with a lower seq holds back the older events after it, so none is skipped."""
    db = fresh_db
    tenant = uuid4()
    first = await _write(db, tenant, resource_id="res-0")
    second = await _write(db, tenant, resource_id="res-1")
    third = await _write(db, tenant, resource_id="res-2")
    scope = None if instance_scope else tenant
    # `first` stays recent (as a late commit would be); `second` and `third` are 10 minutes old.
    await _run_owner(
        db,
        "UPDATE audit_events SET recorded_at = now() - interval '10 minutes' WHERE id = ANY(:ids)",
        ids=[second, third],
    )
    assert await _export(db, scope, 60.0) == []
    await _run_owner(
        db,
        "UPDATE audit_events SET recorded_at = now() - interval '10 minutes' WHERE id = :i",
        i=first,
    )
    lines = await _export(db, scope, 60.0)
    assert [json.loads(line)["id"] for line in lines] == [str(first), str(second), str(third)]


# --- emitters (NG6f): ADR 0032 invariants 5, 6, 9, 10 against the real table ----------------

CANARY = "canary-secret-7f3a9c1e"


async def _tenant_events(db: Db, tenant: UUID) -> list[Any]:
    async with tenant_session(db.app, tenant) as session:
        page = await list_events(session, AuditQuery(limit=100), tenant_id=tenant)
    return page.items


async def test_the_writer_stores_an_event_in_its_tenants_context(db: Db) -> None:
    """A tenant event is inserted in the tenant context and seen only by that tenant."""
    tenant, other = uuid4(), uuid4()
    await AuditWriter(db.app).record(
        _event(tenant, actor_type=ActorType.SYSTEM, actor_sub="worker", actor_iss=None)
    )
    (record,) = await _tenant_events(db, tenant)
    assert (record.actor_sub, record.tenant_id) == ("worker", tenant)
    assert await _tenant_events(db, other) == []


async def test_a_denial_is_a_null_tenant_row_that_only_instance_scope_reads(db: Db) -> None:
    """Inv. 3a, 10: the app role writes it with no context; a tenant never sees it."""
    sub = f"denied-{uuid4()}"
    requested = str(uuid4())
    denial = Denial(
        sub=sub,
        iss="https://idp.example",
        reason="tenant_not_granted",
        required_role="operator",
        scope="tenant",
        tenant_id=requested,
        source_ip="203.0.113.4",
        request_id="req-9",
    )
    await AuditDenials(AuditWriter(db.app)).denied(denial)
    async with db.app() as session:
        page = await list_events(session, AuditQuery(actor_sub=sub), instance_scope=True)
    (record,) = page.items
    assert record.tenant_id is None
    assert (record.action, record.outcome) == (Action.AUTHZ_DENIED, Outcome.DENIED)
    assert (record.resource_type, record.resource_id) == ("tenant", requested)
    assert (record.actor_sub, record.actor_iss) == (sub, "https://idp.example")
    assert (record.source_ip, record.request_id) == ("203.0.113.4", "req-9")
    assert record.after == {
        "reason": "tenant_not_granted",
        "required_role": "operator",
        "scope": "tenant",
    }
    assert await _tenant_events(db, UUID(requested)) == []


async def test_a_tenant_sync_event_records_the_state_and_the_sender(db: Db) -> None:
    """The system actor `github-webhook`, the state after the sync and the sender, no login."""
    tenant = uuid4()
    await TenantSyncAudit(AuditWriter(db.app)).tenant_synced(
        tenant_id=tenant, state="suspended", delivery_id="d-77", sender_id=583231
    )
    (record,) = await _tenant_events(db, tenant)
    assert (record.actor_type, record.actor_sub, record.actor_iss) == (
        ActorType.SYSTEM,
        "github-webhook",
        None,
    )
    assert (record.action, record.resource_id) == (Action.TENANT_SYNC, str(tenant))
    assert record.after == {"state": "suspended"}
    assert record.on_behalf_of == {"github_sender_id": 583231, "delivery_id": "d-77"}
    assert record.source_ip is None


async def test_a_tenant_sync_without_a_sender_stores_a_null_on_behalf_of(db: Db) -> None:
    """No numeric sender: the column is SQL null, not a JSON null or an empty object."""
    tenant = uuid4()
    await TenantSyncAudit(AuditWriter(db.app)).tenant_synced(
        tenant_id=tenant, state="active", delivery_id="d-78", sender_id=None
    )
    (record,) = await _tenant_events(db, tenant)
    assert record.on_behalf_of is None
    is_null = await _scalar(
        db.owner,
        "SELECT on_behalf_of IS NULL FROM audit_events WHERE tenant_id = CAST(:t AS uuid)",
        t=tenant,
    )
    assert is_null is True


async def test_on_behalf_of_is_a_jsonb_column(db: Db) -> None:
    """The migration chain ends with `on_behalf_of` as jsonb, holding the object as written."""
    assert (
        await _admin_scalar(
            db,
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'audit_events' AND column_name = 'on_behalf_of'",
        )
        == "jsonb"
    )
    tenant = uuid4()
    await _write(db, tenant, on_behalf_of={"github_sender_id": 7, "delivery_id": "d"})
    sender = await _scalar(
        db.owner,
        "SELECT on_behalf_of->>'github_sender_id' FROM audit_events "
        "WHERE tenant_id = CAST(:t AS uuid)",
        t=tenant,
    )
    assert sender == "7"


def _run_migration(sync_conn: Connection, direction: str) -> None:
    """Run the `on_behalf_of` revision's `upgrade` or `downgrade` on `sync_conn`."""
    path = next((ROOT / "alembic" / "versions").glob("f6d2b8c14a39_*.py"))
    spec = importlib.util.spec_from_file_location("audit_on_behalf_of_revision", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(sync_conn)):
        getattr(module, direction)()


_COLUMN_TYPE = (
    "SELECT data_type FROM information_schema.columns "
    "WHERE table_name = 'audit_events' AND column_name = 'on_behalf_of'"
)
_SNAPSHOT = (
    "SELECT actor_sub, on_behalf_of FROM audit_events "
    "WHERE actor_sub LIKE 'migration-%' ORDER BY actor_sub"
)
_OBJECT_INSERT = (
    "INSERT INTO audit_events (id, actor_type, actor_sub, action, resource_type, outcome, "
    "on_behalf_of) VALUES (gen_random_uuid(), 'system', 'migration-obj', 'tenant.sync', "
    "'tenant', 'success', CAST(:v AS jsonb))"
)
_LEGACY_INSERT = (
    "INSERT INTO audit_events (id, actor_type, actor_sub, action, resource_type, outcome, "
    "on_behalf_of) VALUES (gen_random_uuid(), 'system', :sub, 'tenant.sync', 'tenant', "
    "'success', :v)"
)


async def _snapshot(conn: AsyncConnection) -> dict[str, object]:
    """Every migration-test row's `on_behalf_of`, by `actor_sub`."""
    return {row[0]: row[1] for row in (await conn.execute(text(_SNAPSHOT))).all()}


async def _migration_steps(conn: AsyncConnection) -> dict[str, object]:
    """Walk the revision down and up with legacy and object rows; each step's full snapshot."""
    steps: dict[str, object] = {}
    await conn.run_sync(_run_migration, "downgrade")
    steps["column before"] = (await conn.execute(text(_COLUMN_TYPE))).scalar_one()
    for suffix, value in (("a", "sender=1;delivery=d"), ("b", None), ("c", "garbage")):
        await conn.execute(text(_LEGACY_INSERT), {"sub": f"migration-{suffix}", "v": value})
    await conn.run_sync(_run_migration, "upgrade")
    steps["upgraded"] = await _snapshot(conn)
    await conn.execute(
        text(_OBJECT_INSERT), {"v": json.dumps({"github_sender_id": 9, "delivery_id": "d-9"})}
    )
    steps["with object"] = await _snapshot(conn)
    await conn.run_sync(_run_migration, "downgrade")
    steps["downgraded"] = await _snapshot(conn)
    await conn.run_sync(_run_migration, "upgrade")
    steps["re-upgraded"] = await _snapshot(conn)
    return steps


async def test_the_on_behalf_of_revision_converts_legacy_text_and_round_trips_losslessly(
    fresh_db: Db,
) -> None:
    """Up turns legacy text into the object (garbage into null); down writes the legacy text back.

    Going up again restores every row exactly: nothing is lost and nothing becomes null.
    """
    try:
        async with fresh_db.admin.connect() as conn:
            steps = await _migration_steps(conn)
            await conn.rollback()
    except DBAPIError as exc:
        if "permission" in str(exc) or "must be owner" in str(exc):
            pytest.skip("the test database user cannot alter the re-owned table")
        raise
    first = {
        "migration-a": {"github_sender_id": 1, "delivery_id": "d"},
        "migration-b": None,
        "migration-c": None,
    }
    both = {**first, "migration-obj": {"github_sender_id": 9, "delivery_id": "d-9"}}
    assert steps == {
        "column before": "text",
        "upgraded": first,
        "with object": both,
        "downgraded": {
            "migration-a": "sender=1;delivery=d",
            "migration-b": None,
            "migration-c": None,
            "migration-obj": "sender=9;delivery=d-9",
        },
        "re-upgraded": both,
    }


@pytest.mark.parametrize(
    "bad",
    [
        '"a bare string"',
        "[1, 2]",
        "{}",
        '{"github_sender_id": 1}',
        '{"github_sender_id": "1", "delivery_id": "d"}',
        '{"github_sender_id": 1, "delivery_id": 5}',
    ],
)
async def test_the_database_refuses_an_on_behalf_of_of_the_wrong_shape(db: Db, bad: str) -> None:
    """The CHECK holds for every insert path, not only the application's validation."""
    with pytest.raises(DBAPIError, match="ck_audit_events_on_behalf_of"):
        await _run_owner(
            db,
            "INSERT INTO audit_events (id, actor_type, actor_sub, action, resource_type, outcome, "
            "on_behalf_of) VALUES (gen_random_uuid(), 'system', 'worker', 'tenant.sync', "
            "'tenant', 'success', CAST(:v AS jsonb))",
            v=bad,
        )


def _rotation(tenant: UUID) -> tuple[StoredCredential, Operation]:
    scope = ScopeId(repository_id=10, repository_owner_id=5, environment="prod", permission="push")
    slot = CredentialSlot(
        generation=2, name=f"tb-{CANARY}-g2", ref={"robot_id": CANARY}, delivered=True
    )
    credential = StoredCredential(
        tenant_id=tenant,
        adapter="ghcr",
        scope_key="k1",
        scope=scope,
        kind="secret",
        state=CredentialState(current=slot),
    )
    operation = Operation(
        id=uuid4(),
        tenant_id=tenant,
        adapter="ghcr",
        scope_key="k1",
        kind=OperationKind.ROTATE,
        status=OperationStatus.INTENT,
        oidc_offered=False,
        base_state=CredentialState(current=slot),
        base_kind="secret",
        error=f"adapter said token={CANARY}",
    )
    return credential, operation


async def test_a_credential_event_commits_with_its_change_and_not_without_it(db: Db) -> None:
    """Inv. 9: the observer appends in the save's session; a rollback leaves no event."""
    tenant = uuid4()
    credential, operation = _rotation(tenant)
    observer = CredentialAuditObserver(AuditWriter(db.app), SystemActor.RECONCILER)
    with pytest.raises(RuntimeError, match="change failed"):
        async with tenant_session(db.app, tenant) as session, session.begin():
            await observer.saved(
                session,
                previous_kind="secret",
                previous_status=None,
                credential=credential,
                operation=operation,
            )
            raise RuntimeError("change failed")
    assert await _tenant_events(db, tenant) == []
    async with tenant_session(db.app, tenant) as session, session.begin():
        await observer.saved(
            session,
            previous_kind="secret",
            previous_status=None,
            credential=credential,
            operation=operation,
        )
    (record,) = await _tenant_events(db, tenant)
    assert (record.action, record.actor_sub, record.resource_id) == (
        Action.CREDENTIAL_ROTATE_BEGIN,
        "reconciler",
        "k1",
    )
    assert record.after is not None
    assert record.after["operation_id"] == str(operation.id)


async def test_a_canary_in_a_credential_name_reference_or_error_reaches_no_row(db: Db) -> None:
    """Inv. 6: the canary appears in no column of any row (save events and the sweep's)."""
    tenant = uuid4()
    credential, operation = _rotation(tenant)
    observer = CredentialAuditObserver(AuditWriter(db.app), SystemActor.WORKER)
    async with tenant_session(db.app, tenant) as session, session.begin():
        await observer.saved(
            session,
            previous_kind=None,
            previous_status=None,
            credential=credential,
            operation=operation,
        )
    await observer.orphan_deleted(tenant, "ghcr", "tb-orphan-k1-g1")
    assert len(await _tenant_events(db, tenant)) == 2  # rotate begin and the orphan sweep
    leaked = await _admin_scalar(
        db,
        "SELECT count(*) FROM audit_events "
        "WHERE row_to_json(audit_events)::text LIKE '%canary-secret-7f3a9c1e%'",
    )
    assert leaked == 0
