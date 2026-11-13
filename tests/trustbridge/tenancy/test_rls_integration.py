"""Row-level security, the tenant hook and the definer functions against a real Postgres.

ADR 0031 invariants for release N (NG6a). Runs in CI's stack (integration), never in the unit
lane. Each fixture provisions a throwaway database, migrates it to head with the real Alembic
chain (as the test database's own user, then hands the tenant-owned tables and the functions to
`trustbridge_owner`), and connects as `trustbridge_app`, which is neither the owner nor a
superuser. Release N ships the policies inert, so `enforced_db` turns RLS on and forces it
itself (what release N+1's migration does) to prove the policies do what they say;
`inert_db` is the release-N schema as shipped.
"""

import asyncio
import importlib.util
import os
import re
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Literal, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import (
    Connection,
    CursorResult,
    Result,
    column,
    delete,
    func,
    select,
    table,
    text,
    update,
)
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from sqlalchemy.sql import Executable

from app.config import Settings
from app.db import build_engine
from trustbridge.tenancy import (
    current_tenant,
    for_each_tenant,
    install_tenant_hook,
    iterate_tenants,
    list_tenants,
    tenant_context,
    tenant_session,
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
APP_ROLE = "trustbridge_app"
OWNER_ROLE = "trustbridge_owner"
PASSWORD = "tb-tenancy-test"  # noqa: S105 -- a throwaway test role in a throwaway database
KEYED = (
    "tenant_repositories",
    "scope_leases",
    "adapter_resources",
    "adapter_credentials",
    "credential_operations",
)
SIX = ("tenants", *KEYED)
FUNCTIONS = (
    "tb_current_tenant()",
    "tb_resolve_tenant(text, bigint)",
    "tb_upsert_tenant(text, bigint, text, bigint, text)",
    "tb_list_tenants()",
)
RLS_ERROR = "row-level security"
# One row per keyed table; `:k` is the key part (so a test row never collides with the seed row)
# and `:n` a number.
INSERTS = {
    "tenant_repositories": (
        "INSERT INTO tenant_repositories (tenant_id, repository_id, full_name) "
        "VALUES (:t, :n, 'o/r')"
    ),
    "scope_leases": (
        "INSERT INTO scope_leases (tenant_id, adapter, lease_key, holder, fence, expires_at) "
        "VALUES (:t, 'a', :k, 'h', :n, now() + interval '1 hour')"
    ),
    "adapter_resources": (
        "INSERT INTO adapter_resources (tenant_id, adapter, resource_key, state) "
        "VALUES (:t, 'a', :k, CAST('{}' AS jsonb))"
    ),
    "adapter_credentials": (
        "INSERT INTO adapter_credentials (tenant_id, adapter, scope_key, scope, state) "
        "VALUES (:t, 'a', :k, CAST('{}' AS jsonb), CAST('{}' AS jsonb))"
    ),
    "credential_operations": (
        "INSERT INTO credential_operations "
        "(id, tenant_id, adapter, scope_key, kind, status, oidc_offered) "
        "VALUES (gen_random_uuid(), :t, 'a', :k, 'issue', 'pending', false)"
    ),
}
ROLES_SQL = """
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trustbridge_app') THEN
        CREATE ROLE trustbridge_app LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trustbridge_owner') THEN
        CREATE ROLE trustbridge_owner LOGIN;
    END IF;
END $$
"""


@dataclass(slots=True)
class Db:
    """A provisioned scratch database and the engines over it."""

    name: str
    a: UUID
    b: UUID
    admin: AsyncEngine
    app: async_sessionmaker[AsyncSession]
    owner: async_sessionmaker[AsyncSession]


def _url(user: str, password: str, database: str) -> URL:
    s = Settings()
    return URL.create(
        "postgresql+asyncpg",
        username=user,
        password=password,
        host=s.postgres_host,
        port=s.postgres_port,
        database=database,
    )


def _app_engine(
    database: str,
    user: str = APP_ROLE,
    mode: Literal["null", "local", "external"] = "null",
    size: int | None = None,
) -> AsyncEngine:
    """The application's own engine builder, pointed at the scratch database as `user`."""
    return build_engine(
        Settings(
            postgres_user=user,
            postgres_password=PASSWORD,
            postgres_db=database,
            db_pool_mode=mode,
            db_pool_size=size,
            db_max_overflow=0 if size is not None else None,
        )
    )


async def _provision(*, enforce: bool) -> AsyncIterator[Db]:
    """Create, migrate and seed a scratch database; drop it afterwards."""
    s = Settings()
    name = f"tb_tenancy_{uuid4().hex[:10]}"
    server = create_async_engine(
        _url(s.postgres_user, s.postgres_password, s.postgres_db),
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    try:
        async with server.connect() as conn:
            await conn.execute(text(ROLES_SQL))
            for role in (APP_ROLE, OWNER_ROLE):
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
    admin = create_async_engine(
        _url(s.postgres_user, s.postgres_password, name), poolclass=NullPool
    )
    engines = [admin, _app_engine(name), _app_engine(name, OWNER_ROLE)]
    try:
        async with admin.begin() as conn:
            for t in SIX:
                await conn.execute(text(f"ALTER TABLE {t} OWNER TO {OWNER_ROLE}"))
            # `tb_delete_tenant` (owned by the owner role in the tests that run it) reads this
            # table; in a deployment the migrate Job runs as the owner role, which owns it.
            await conn.execute(text(f"ALTER TABLE webhook_deliveries OWNER TO {OWNER_ROLE}"))
            for function in FUNCTIONS:
                await conn.execute(text(f"ALTER FUNCTION {function} OWNER TO {OWNER_ROLE}"))
            if enforce:
                for t in SIX:
                    await conn.execute(text(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY"))
                    await conn.execute(text(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY"))
            ids: list[UUID] = []
            for number, org in ((1, "a"), (2, "b")):
                tenant_id = (
                    await conn.execute(
                        text(
                            "INSERT INTO tenants (host, installation_id, org, org_id, state) "
                            "VALUES ('tb.test', :i, :o, :i, 'active') RETURNING id"
                        ),
                        {"i": number, "o": org},
                    )
                ).scalar_one()
                ids.append(tenant_id)
                for insert in INSERTS.values():
                    await conn.execute(text(insert), {"t": tenant_id, "k": "seed", "n": 1})
        yield Db(
            name,
            ids[0],
            ids[1],
            admin,
            async_sessionmaker(engines[1], expire_on_commit=False),
            async_sessionmaker(engines[2], expire_on_commit=False),
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
async def enforced_db() -> AsyncIterator[Db]:
    """The release-N schema with RLS enabled and forced, as release N+1 leaves it."""
    async for db in _provision(enforce=True):
        yield db


@pytest.fixture(scope="module")
async def inert_db() -> AsyncIterator[Db]:
    """The release-N schema as shipped: policies exist, RLS is not enabled."""
    async for db in _provision(enforce=False):
        yield db


def _rowcount(result: Result[Any]) -> int:
    return cast("CursorResult[Any]", result).rowcount


def _migration() -> ModuleType:
    path = next((ROOT / "alembic" / "versions").glob("d4b8f1e60a27_*.py"))
    spec = importlib.util.spec_from_file_location("ng6a_migration", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _admin_scalar(db: Db, statement: Executable) -> object:
    async with db.admin.begin() as conn:
        return (await conn.execute(statement)).scalar_one()


async def _values(session: AsyncSession, name: str, key: str) -> list[Any]:
    result: Result[Any] = await session.execute(select(column(key)).select_from(table(name)))
    return list(result.scalars().all())


async def _count(session: AsyncSession, name: str) -> int:
    return (await session.execute(select(func.count()).select_from(table(name)))).scalar_one()


async def _isolation_holds(factory: async_sessionmaker[AsyncSession], a: UUID) -> bool:
    """True when, in tenant `a`'s context, every one of the six tables shows only `a`'s rows."""
    for name in SIX:
        key = "id" if name == "tenants" else "tenant_id"
        async with tenant_session(factory, a) as session:
            rows = await _values(session, name, key)
        if not rows or set(rows) != {a}:
            return False
    return True


# --- context and hook (inv. 1-5) -----------------------------------------------------------


async def test_in_a_context_only_that_tenants_rows_are_visible_and_foreign_writes_fail(
    enforced_db: Db,
) -> None:  # inv. 1
    """Reads see only tenant A's rows; an INSERT or UPDATE naming tenant B's id fails."""
    db = enforced_db
    assert await _isolation_holds(db.app, db.a)
    for name, insert in INSERTS.items():
        async with tenant_session(db.app, db.a) as session:  # a's own insert is allowed
            result = await session.execute(text(insert), {"t": db.a, "k": "x", "n": 2})
            assert _rowcount(result) == 1
            await session.rollback()
        async with tenant_session(db.app, db.a) as session:
            with pytest.raises(DBAPIError, match=RLS_ERROR):
                await session.execute(text(insert), {"t": db.b, "k": "x", "n": 2})
        async with tenant_session(db.app, db.a) as session:
            stolen = (
                update(table(name, column("tenant_id")))
                .where(column("tenant_id") == db.a)
                .values(tenant_id=db.b)
            )
            with pytest.raises(DBAPIError, match=RLS_ERROR):
                await session.execute(stolen)


async def test_with_no_context_every_table_is_empty_and_every_write_fails(
    enforced_db: Db,
) -> None:  # inv. 2
    """No tenant: no rows, INSERTs rejected, UPDATE/DELETE touch nothing, no connection error."""
    db = enforced_db
    for name in SIX:
        async with db.app() as session:
            assert await _count(session, name) == 0
    for insert in INSERTS.values():
        async with db.app() as session:
            with pytest.raises(DBAPIError, match=RLS_ERROR):
                await session.execute(text(insert), {"t": db.a, "k": "x", "n": 2})
    async with db.app() as session:
        with pytest.raises(DBAPIError, match=RLS_ERROR):
            await session.execute(
                text("INSERT INTO tenants (host, installation_id) VALUES ('tb.test', 99)")
            )
    for name in KEYED:
        async with db.app() as session:
            changed = await session.execute(
                update(table(name, column("tenant_id"))).values(tenant_id=db.a)
            )
            removed = await session.execute(delete(table(name)))
            assert (_rowcount(changed), _rowcount(removed)) == (0, 0)
    assert await _admin_scalar(db, select(func.count()).select_from(table("scope_leases"))) == 2


@pytest.mark.parametrize("mode", ["local", "external"])
async def test_the_setting_is_cleared_when_a_transaction_ends_and_a_pooled_connection_is_reused(
    enforced_db: Db, mode: Literal["local", "external"]
) -> None:  # inv. 3
    """After commit and rollback the next transaction on the connection sees no tenant."""
    db = enforced_db
    engine = _app_engine(db.name, mode=mode, size=1 if mode == "local" else None)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    probe = text("SELECT pg_backend_pid(), current_setting('app.tenant_id', true)")
    try:
        async with tenant_session(factory, db.a) as session:
            pid_a, during = (await session.execute(probe)).one()
            await session.commit()
        assert during == str(db.a)
        async with tenant_session(factory, db.b) as session:
            pid_b, during_b = (await session.execute(probe)).one()
            await session.rollback()
        assert during_b == str(db.b)
        async with factory() as session:  # no context
            pid_c, after = (await session.execute(probe)).one()
        assert after in ("", None)
        async with engine.connect() as conn:  # no session, so no hook: nothing is left over
            leftover = (
                await conn.execute(text("SELECT current_setting('app.tenant_id', true)"))
            ).scalar_one()
        assert leftover in ("", None)
        if mode == "local":  # one pooled connection served all of them
            assert pid_a == pid_b == pid_c
    finally:
        await engine.dispose()


async def test_concurrent_tenant_sessions_never_see_each_others_context(
    enforced_db: Db,
) -> None:  # inv. 4
    """Two interleaved sessions keep their own setting and rows across awaits."""
    db = enforced_db

    async def probe(tenant_id: UUID) -> tuple[str, str, set[UUID], UUID | None]:
        async with tenant_session(db.app, tenant_id) as session:
            first = (
                await session.execute(text("SELECT current_setting('app.tenant_id')"))
            ).scalar_one()
            await asyncio.sleep(0.05)
            rows = await _values(session, "scope_leases", "tenant_id")
            later = (
                await session.execute(text("SELECT current_setting('app.tenant_id')"))
            ).scalar_one()
            hopped = await asyncio.to_thread(current_tenant.get)
        return first, later, set(rows), hopped

    one, two = await asyncio.gather(probe(db.a), probe(db.b))
    assert one == (str(db.a), str(db.a), {db.a}, db.a)
    assert two == (str(db.b), str(db.b), {db.b}, db.b)


async def test_a_handler_never_inherits_a_transaction_begun_before_its_context(
    enforced_db: Db,
) -> None:  # inv. 5 (ordering)
    """A transaction begun with no context stays empty; one begun inside the context sees rows."""
    db = enforced_db
    async with db.app() as session:
        await session.execute(text("SELECT 1"))  # the (RBAC-style) transaction, begun first
        with tenant_context(db.a):
            early = await _count(session, "scope_leases")
            setting = (
                await session.execute(text("SELECT current_setting('app.tenant_id', true)"))
            ).scalar_one()
            assert (early, setting) == (0, "")
            await session.rollback()  # closed; the next one begins inside the context
            assert await _count(session, "scope_leases") == 1


async def test_list_tenants_and_tenant_session_round_trip(enforced_db: Db) -> None:
    """`list_tenants` reads every tenant with no context through `tb_list_tenants()`."""
    db = enforced_db
    found = {t.id: t for t in await list_tenants(db.app)}
    assert {db.a, db.b} <= set(found)
    assert found[db.a].org == "a"
    assert (found[db.a].admin_disabled_at, found[db.a].deleting) == (None, False)
    assert current_tenant.get() is None


# --- policies and functions (inv. 6-10) ----------------------------------------------------


async def test_the_app_role_cannot_insert_or_delete_a_tenant_and_updates_only_its_own_row(
    enforced_db: Db,
) -> None:  # inv. 6 (the tb_delete_tenant part moves to NG6h)
    """Direct INSERT fails, DELETE removes nothing, UPDATE reaches only the context's row."""
    db = enforced_db
    async with tenant_session(db.app, db.a) as session:
        with pytest.raises(DBAPIError, match=RLS_ERROR):
            await session.execute(
                text("INSERT INTO tenants (host, installation_id) VALUES ('tb.test', 98)")
            )
    async with tenant_session(db.app, db.a) as session:
        assert _rowcount(await session.execute(delete(table("tenants")))) == 0
    async with tenant_session(db.app, db.a) as session:
        seen = await _values(session, "tenants", "id")
        assert seen == [db.a]
        changed = await session.execute(
            update(table("tenants", column("state"))).values(state="suspended")
        )
        assert _rowcount(changed) == 1
        await session.commit()
    states = {
        row.id: row.state
        for row in (await _admin_rows(db, "SELECT id, state FROM tenants WHERE host = 'tb.test'"))
    }
    assert states == {db.a: "suspended", db.b: "active"}
    async with db.admin.begin() as conn:
        await conn.execute(text("UPDATE tenants SET state = 'active' WHERE host = 'tb.test'"))
    # creating and resolving go through the functions only
    async with db.app() as session, session.begin():
        created = (
            await session.execute(text("SELECT tb_upsert_tenant('tb.fn', 7, 'o', 7, 'active')"))
        ).scalar_one()
    async with db.app() as session:
        assert await _count(session, "tenants") == 0
        resolved = (
            await session.execute(text("SELECT tb_resolve_tenant('tb.fn', 7)"))
        ).scalar_one()
    assert resolved == created
    async with db.admin.begin() as conn:
        await conn.execute(text("DELETE FROM tenants WHERE host = 'tb.fn'"))


async def _admin_rows(db: Db, statement: str) -> list[Any]:
    async with db.admin.begin() as conn:
        return list((await conn.execute(text(statement))).all())


async def test_upsert_is_create_or_update_and_resolve_returns_null_for_an_unknown_pair(
    enforced_db: Db,
) -> None:  # inv. 7 (the admin-disable columns arrive with NG6e)
    """`tb_upsert_tenant` keeps one id per pair and rewrites org, org id and state only."""
    db = enforced_db
    upsert = text("SELECT tb_upsert_tenant('tb.up', 5, :org, :org_id, :state)")
    async with db.app() as session, session.begin():
        first = (
            await session.execute(upsert, {"org": "o1", "org_id": 1, "state": "active"})
        ).scalar_one()
    async with db.app() as session, session.begin():
        second = (
            await session.execute(upsert, {"org": "o2", "org_id": 2, "state": "suspended"})
        ).scalar_one()
    assert first == second
    row = (await _admin_rows(db, "SELECT org, org_id, state FROM tenants WHERE host = 'tb.up'"))[0]
    assert tuple(row) == ("o2", 2, "suspended")
    async with db.app() as session:
        assert (
            await session.execute(text("SELECT tb_resolve_tenant('tb.up', 404)"))
        ).scalar_one() is None
    async with db.app() as session:
        with pytest.raises(DBAPIError, match="invalid tenant state"):
            await session.execute(upsert, {"org": "o", "org_id": 1, "state": "bogus"})
    async with db.admin.begin() as conn:
        await conn.execute(text("DELETE FROM tenants WHERE host = 'tb.up'"))


async def test_host_scoped_tables_have_no_rls_and_work_without_a_context(
    enforced_db: Db,
) -> None:  # inv. 8
    """`webhook_deliveries` and `github_host_registrations` are readable and writable bare."""
    db = enforced_db
    flags = await _admin_rows(
        db,
        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE relname IN ('webhook_deliveries', 'github_host_registrations')",
    )
    assert sorted(tuple(r) for r in flags) == [
        ("github_host_registrations", False, False),
        ("webhook_deliveries", False, False),
    ]
    async with db.app() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO webhook_deliveries (delivery_id, host, event, headers) "
                "VALUES ('d1', 'github.com', 'push', CAST('{}' AS jsonb))"
            )
        )
        await session.execute(
            text(
                "INSERT INTO github_host_registrations (host_name, app_id, webhook_secret_ref) "
                "VALUES ('h1', 1, 'ref')"
            )
        )
    async with db.app() as session, session.begin():
        assert await _count(session, "webhook_deliveries") == 1
        assert await _count(session, "github_host_registrations") == 1
        await session.execute(delete(table("webhook_deliveries")))
        await session.execute(delete(table("github_host_registrations")))


async def test_rls_is_forced_and_the_owner_with_its_policy_still_reads_everything(
    enforced_db: Db,
) -> None:  # inv. 9
    """FORCE is set on the six tables; the owner role reads all rows and the functions work."""
    db = enforced_db
    flags = await _admin_rows(
        db,
        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE relname IN ('tenants', 'tenant_repositories', 'scope_leases', 'adapter_resources', "
        "'adapter_credentials', 'credential_operations') AND relkind = 'r'",
    )
    assert sorted(r.relname for r in flags) == sorted(SIX)
    assert all(r.relrowsecurity and r.relforcerowsecurity for r in flags)
    for name in SIX:
        async with db.owner() as session:  # no context, yet all rows: the owner_all policy
            assert await _count(session, name) == 2
    async with db.app() as session:  # a definer function (owned by the owner) lists them too
        ids = (await session.execute(text("SELECT id FROM tb_list_tenants()"))).scalars().all()
    assert {db.a, db.b} <= set(ids)


async def test_definer_functions_have_a_fixed_search_path_and_ignore_a_shadowing_schema(
    enforced_db: Db,
) -> None:  # inv. 10
    """Each definer pins `pg_catalog, public, pg_temp`; a shadow `tenants` table is not used."""
    db = enforced_db
    rows = await _admin_rows(
        db,
        "SELECT proname, prosecdef, proconfig FROM pg_proc "
        "WHERE proname IN ('tb_resolve_tenant', 'tb_upsert_tenant', 'tb_list_tenants')",
    )
    assert len(rows) == 3
    for row in rows:
        assert row.prosecdef is True
        assert "search_path=pg_catalog, public, pg_temp" in row.proconfig
    async with db.admin.begin() as conn:
        for statement in (
            "CREATE SCHEMA evil",
            "CREATE TABLE evil.tenants (LIKE public.tenants INCLUDING DEFAULTS)",
            "INSERT INTO evil.tenants (host, installation_id) VALUES ('shadow', 999)",
            f"GRANT USAGE ON SCHEMA evil TO {APP_ROLE}",
            f"GRANT SELECT ON evil.tenants TO {APP_ROLE}",
        ):
            await conn.execute(text(statement))
    try:
        async with db.app() as session:
            await session.execute(text("SET LOCAL search_path = evil, public"))
            shadowed = await session.execute(
                text("SELECT count(*) FROM tenants WHERE host = 'shadow'")
            )
            assert shadowed.scalar_one() == 1  # the attack is in effect for unqualified names
            resolved = await session.execute(text("SELECT tb_resolve_tenant('shadow', 999)"))
            assert resolved.scalar_one() is None
            listed = await session.execute(
                text("SELECT count(*) FROM tb_list_tenants() WHERE host = 'shadow'")
            )
            assert listed.scalar_one() == 0
    finally:
        async with db.admin.begin() as conn:
            await conn.execute(text("DROP SCHEMA evil CASCADE"))


async def test_execute_on_the_functions_is_granted_to_the_two_roles_and_not_to_public(
    inert_db: Db,
) -> None:
    """The app and owner roles may execute each function; an unrelated role may not."""
    db = inert_db
    probe = "SELECT has_function_privilege(:role, CAST(:fn AS regprocedure), 'EXECUTE')"
    async with db.admin.connect() as conn:
        await conn.execute(text("CREATE ROLE tb_unrelated NOLOGIN"))
        try:
            for function in FUNCTIONS:
                for role, expected in (
                    (APP_ROLE, True),
                    (OWNER_ROLE, True),
                    ("tb_unrelated", False),
                ):
                    got = (
                        await conn.execute(text(probe), {"role": role, "fn": function})
                    ).scalar_one()
                    assert got is expected, (role, function)
        finally:
            await conn.rollback()
            await conn.execute(text("DROP ROLE IF EXISTS tb_unrelated"))
            await conn.commit()


# --- iteration (inv. 11, 12) ---------------------------------------------------------------


async def test_iterate_tenants_yields_every_tenant_once_each_inside_its_own_context(
    enforced_db: Db,
) -> None:  # inv. 11, inv. 12
    """Each tenant once; a sweep-shaped read sees only that tenant's rows despite foreign ones."""
    db = enforced_db
    seen: list[UUID] = []
    async with aclosing(iterate_tenants(db.app)) as tenants:
        async for tenant_id in tenants:
            assert current_tenant.get() == tenant_id
            seen.append(tenant_id)
            async with db.app() as session:
                owners = await _values(session, "scope_leases", "tenant_id")
            assert owners == [tenant_id]  # the planted foreign row is not visible
    assert sorted(seen) == sorted(set(seen))
    assert {db.a, db.b} <= set(seen)
    assert current_tenant.get() is None


async def test_one_tenants_exception_does_not_stop_the_others(enforced_db: Db) -> None:  # inv. 11
    """`for_each_tenant` records the failing tenant and finishes the rest."""
    db = enforced_db
    done: list[UUID] = []

    async def work(tenant_id: UUID) -> None:
        if tenant_id == db.a:
            raise RuntimeError("tenant a fails")
        done.append(tenant_id)

    result = await for_each_tenant(db.app, work)
    assert result.failed == (db.a,)
    assert db.b in done
    assert result.succeeded == len(done)
    assert current_tenant.get() is None


# --- rollout and roles (inv. 13, 15, 17) ---------------------------------------------------


async def test_the_release_n_schema_has_policies_but_rls_is_off_so_nothing_is_filtered(
    inert_db: Db,
) -> None:  # inv. 13
    """RLS is not enabled; the policies exist; the app role sees every row with no context."""
    db = inert_db
    flags = await _admin_rows(
        db,
        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE relname IN ('tenants', 'tenant_repositories', 'scope_leases', 'adapter_resources', "
        "'adapter_credentials', 'credential_operations') AND relkind = 'r'",
    )
    assert sorted(r.relname for r in flags) == sorted(SIX)
    assert not any(r.relrowsecurity or r.relforcerowsecurity for r in flags)
    policies = await _admin_rows(
        db, "SELECT tablename, policyname FROM pg_policies WHERE schemaname = 'public'"
    )
    names = sorted((r.tablename, r.policyname) for r in policies)
    expected = sorted(
        [(t, "tenant_isolation") for t in KEYED]
        + [(t, "owner_all") for t in SIX]
        + [("tenants", "tenant_isolation_select"), ("tenants", "tenant_isolation_update")]
        # audit_events (NG6c, ADR 0032) is created with its policies and RLS forced.
        + [("audit_events", p) for p in ("audit_insert", "audit_select", "owner_all")]
    )
    assert names == expected
    for name in SIX:
        async with db.app() as session:  # no context, and still every row
            assert await _count(session, name) == 2
    async with db.app() as session:
        setting = (await session.execute(text("SELECT tb_current_tenant()"))).scalar_one()
        version = (await session.execute(text("SELECT version FROM schema_compat"))).scalar_one()
    assert setting is None
    assert version >= 1  # written with GREATEST, never bumped by release N


async def test_a_disabled_policy_or_an_owner_connection_fails_the_isolation_probe(
    enforced_db: Db,
) -> None:  # inv. 17
    """A silently inert policy (or connecting as the owner) is caught by the isolation checks."""
    db = enforced_db
    assert await _isolation_holds(db.app, db.a)
    assert not await _isolation_holds(db.owner, db.a)  # the owner sees both tenants
    async with db.admin.begin() as conn:
        await conn.execute(text("DROP POLICY tenant_isolation ON scope_leases"))
    try:
        assert not await _isolation_holds(db.app, db.a)
    finally:
        async with db.admin.begin() as conn:
            await conn.execute(
                text(
                    f"CREATE POLICY tenant_isolation ON scope_leases FOR ALL TO {APP_ROLE} "
                    "USING (tenant_id = (SELECT tb_current_tenant())) "
                    "WITH CHECK (tenant_id = (SELECT tb_current_tenant()))"
                )
            )
    assert await _isolation_holds(db.app, db.a)


async def _check(
    db: Db,
    statements: tuple[str, ...],
    app_role: str = APP_ROLE,
    owner_role: str = OWNER_ROLE,
) -> None:
    """Run `check_roles(production=True, **kwargs)` after `statements`, all rolled back."""
    migration = _migration()

    def run(conn: Connection) -> None:
        migration.check_roles(conn, production=True, app_role=app_role, owner_role=owner_role)

    async with db.admin.connect() as conn:
        trans = await conn.begin()
        try:
            for statement in statements:
                await conn.execute(text(statement))
            await conn.run_sync(run)
        finally:
            await trans.rollback()


async def test_production_refuses_absent_roles_a_bypassing_app_role_or_one_that_owns_a_table(
    inert_db: Db,
) -> None:  # inv. 15
    """Each unsafe role setup is refused with production on, and accepted with it off."""
    db = inert_db
    await _check(db, ())  # the proper setup passes
    migration = _migration()
    async with db.admin.connect() as conn:
        await conn.run_sync(
            lambda c: migration.check_roles(c, production=False, app_role="tb_absent")
        )
    with pytest.raises(RuntimeError, match="does not exist"):
        await _check(db, (), app_role="tb_absent_app")
    with pytest.raises(RuntimeError, match="does not exist"):
        await _check(db, (), owner_role="tb_absent_owner")
    with pytest.raises(RuntimeError, match="superuser"):
        await _check(db, ("CREATE ROLE tb_x_su NOLOGIN SUPERUSER",), app_role="tb_x_su")
    with pytest.raises(RuntimeError, match="BYPASSRLS"):
        await _check(db, ("CREATE ROLE tb_x_by NOLOGIN BYPASSRLS",), app_role="tb_x_by")
    with pytest.raises(RuntimeError, match="owns tenant tables: tenants"):
        await _check(
            db,
            ("CREATE ROLE tb_x_own NOLOGIN", "ALTER TABLE tenants OWNER TO tb_x_own"),
            app_role="tb_x_own",
        )


def test_production_is_read_from_mode_and_no_migration_creates_a_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # inv. 15
    """`MODE=production` switches the checks on, and no revision issues CREATE ROLE."""
    migration = _migration()
    monkeypatch.setenv("MODE", "production")
    assert migration.is_production() is True
    monkeypatch.setenv("MODE", "dev")
    assert migration.is_production() is False
    for path in (ROOT / "alembic" / "versions").glob("*.py"):
        assert not re.search(r"create\s+role", path.read_text(), re.IGNORECASE), path.name
