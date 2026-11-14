"""Environment tracking against a real Postgres with RLS enforced (NG6h-6a, ADR 0034 B6).

A scratch database of its own, as `trustbridge_app`: the store runs through the tenant policies and
the app role's least-privilege grants, so these tests are what prove they are enough.
"""

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.trustbridge.github.conftest import Env
from tests.trustbridge.github.test_tokens_writer import script_repo
from tests.trustbridge.tenancy.test_deletion_db_integration import (
    _admin,
    _admin_rows,
    _admin_scalar,
    _alembic,
)
from tests.trustbridge.tenancy.test_rls_integration import OWNER_ROLE, RLS_ERROR, Db, _provision
from trustbridge.adapters.interface import GitHubOutput
from trustbridge.github.environments import SqlEnvironmentStore
from trustbridge.github.writer import GitHubOutputWriter
from trustbridge.tenancy import install_tenant_hook, tenant_session

pytestmark = pytest.mark.integration

REVISION = "9a4d6c2e8b17"
PREVIOUS = "7b2d4f8a1c36"
PATH = "/repos/acme/web/environments/prod"
ENVIRONMENT = GitHubOutput("environment", "prod", "", 10, None)


@pytest.fixture(scope="module", autouse=True)
def _hook() -> None:
    """The process installs the tenant hook once, as `build_core` and `main.install` do."""
    install_tenant_hook()


@pytest.fixture(scope="module")
async def db() -> AsyncIterator[Db]:
    """A scratch database with RLS enabled and forced on the tenant tables."""
    async for provisioned in _provision(enforce=True):
        async with provisioned.admin.begin() as conn:  # `_provision` enforces the six tables only
            await conn.execute(text(f"ALTER TABLE github_environments OWNER TO {OWNER_ROLE}"))
            await conn.execute(text("ALTER TABLE github_environments ENABLE ROW LEVEL SECURITY"))
            await conn.execute(text("ALTER TABLE github_environments FORCE ROW LEVEL SECURITY"))
        yield provisioned


async def _rows(db: Db, tenant: UUID) -> list[tuple[str, int, str]]:
    found = await _admin_rows(
        db,
        "SELECT host, repository_id, environment_name FROM github_environments "
        "WHERE tenant_id = :t ORDER BY repository_id, environment_name",
        {"t": tenant},
    )
    return [(r.host, r.repository_id, r.environment_name) for r in found]


async def test_a_created_environment_is_recorded_and_a_found_one_is_not(db: Db, env: Env) -> None:
    """Through the real writer and store: 404 then PUT records one row; a 200 records none."""
    script_repo(env)
    env.gh.route("GET", PATH, 404, {})
    env.gh.route("PUT", PATH, 200, {})
    tenant = uuid4()
    store = SqlEnvironmentStore(db.app)
    writer = GitHubOutputWriter(env.gateway, store)
    scoped = type(env.tenant)(id=tenant, github_host=env.tenant.github_host, org="acme")
    await writer.apply(scoped, [ENVIRONMENT])
    assert await _rows(db, tenant) == [("github.com", 10, "prod")]
    # a second apply finds it (200): still one row, and the writer made no second record
    env.gh.route("GET", PATH, 200, {})
    await writer.apply(scoped, [ENVIRONMENT])
    assert await _rows(db, tenant) == [("github.com", 10, "prod")]
    # an environment that was there before records nothing
    other = "/repos/acme/web/environments/staging"
    env.gh.route("GET", other, 200, {})
    env.gh.route("PUT", other, 200, {})
    await writer.apply(scoped, [GitHubOutput("environment", "staging", "", 10, None)])
    assert await _rows(db, tenant) == [("github.com", 10, "prod")]


async def test_recording_is_idempotent_and_refreshes_the_time(db: Db) -> None:
    """The same environment twice is one row; the second call moves `created_at` forward."""
    store = SqlEnvironmentStore(db.app)
    tenant = uuid4()
    await store.record_created(tenant, "tb.test", 7, "qa")
    first = await _admin_scalar(
        db, "SELECT created_at FROM github_environments WHERE tenant_id = :t", {"t": tenant}
    )
    await store.record_created(tenant, "tb.test", 7, "qa")
    second = await _admin_scalar(
        db, "SELECT created_at FROM github_environments WHERE tenant_id = :t", {"t": tenant}
    )
    assert await _rows(db, tenant) == [("tb.test", 7, "qa")]
    assert second > first


async def test_tracked_lists_the_tenants_rows_and_forget_removes_one(db: Db) -> None:
    """NG6h-6's two calls: list in key order, remove one (absent is fine); others untouched."""
    store = SqlEnvironmentStore(db.app)
    a, b = uuid4(), uuid4()
    for repo, name in ((9, "prod"), (3, "stage"), (3, "dev")):
        await store.record_created(a, "tb.test", repo, name)
    await store.record_created(b, "tb.test", 3, "dev")
    assert await store.tracked(a) == [(3, "dev"), (3, "stage"), (9, "prod")]
    await store.forget(a, 3, "dev")
    await store.forget(a, 3, "dev")
    assert await store.tracked(a) == [(3, "stage"), (9, "prod")]
    assert await store.tracked(b) == [(3, "dev")]
    assert await store.tracked(uuid4()) == []


async def test_a_tenant_sees_and_changes_only_its_own_rows(db: Db) -> None:
    """RLS: a context shows only its rows; a foreign insert fails, a foreign delete is empty."""
    mine, theirs = uuid4(), uuid4()
    await _admin(
        db,
        "INSERT INTO github_environments (tenant_id, host, repository_id, environment_name) "
        "VALUES (:m, 'h', 1, 'x'), (:o, 'h', 1, 'x')",
        {"m": mine, "o": theirs},
    )
    async with tenant_session(db.app, mine) as session:
        seen = (await session.execute(text("SELECT tenant_id FROM github_environments"))).scalars()
        assert set(seen) == {mine}
        deleted = await session.execute(
            text("DELETE FROM github_environments WHERE tenant_id = :o RETURNING 1"), {"o": theirs}
        )
        assert deleted.all() == []
    async with tenant_session(db.app, mine) as session:
        with pytest.raises(DBAPIError, match=RLS_ERROR):
            await session.execute(
                text(
                    "INSERT INTO github_environments "
                    "(tenant_id, host, repository_id, environment_name) VALUES (:o, 'h', 2, 'y')"
                ),
                {"o": theirs},
            )
    async with db.app() as session:  # no context: no rows
        count = await session.execute(text("SELECT count(*) FROM github_environments"))
        assert count.scalar_one() == 0
    assert await _rows(db, theirs) == [("h", 1, "x")]


async def test_the_app_role_can_change_only_created_at(db: Db) -> None:
    """Least privilege: `UPDATE (created_at)` is the only update grant (it backs the row locks)."""
    tenant = uuid4()
    await SqlEnvironmentStore(db.app).record_created(tenant, "tb.test", 5, "keep")
    async with tenant_session(db.app, tenant) as session:
        await session.execute(text("UPDATE github_environments SET created_at = now()"))
        await session.commit()
    async with tenant_session(db.app, tenant) as session:
        with pytest.raises(DBAPIError, match="permission denied"):
            await session.execute(text("UPDATE github_environments SET host = 'evil'"))


async def test_the_migration_round_trips_and_the_downgrade_refuses_tracked_rows() -> None:
    """Down drops the table, up recreates it; with a tracked row the downgrade is refused."""
    async for scratch in _provision(enforce=False):
        stepped = await _alembic(scratch.name, "downgrade", PREVIOUS)
        assert stepped.returncode == 0, stepped.stderr[-2000:]
        assert await _admin_scalar(scratch, "SELECT to_regclass('github_environments') IS NULL")
        restored = await _alembic(scratch.name, "upgrade", REVISION)
        assert restored.returncode == 0, restored.stderr[-2000:]
        assert await _admin_scalar(scratch, "SELECT to_regclass('github_environments') IS NOT NULL")
        await SqlEnvironmentStore(scratch.app).record_created(scratch.a, "tb.test", 1, "prod")
        refused = await _alembic(scratch.name, "downgrade", PREVIOUS)
        assert refused.returncode != 0
        assert "tracked github environments exist" in refused.stderr
        assert await _admin_scalar(scratch, "SELECT count(*) FROM github_environments") == 1
        await _admin(scratch, "DELETE FROM github_environments")
        done = await _alembic(scratch.name, "downgrade", PREVIOUS)
        assert done.returncode == 0, done.stderr[-2000:]
        again = await _alembic(scratch.name, "upgrade", "head")
        assert again.returncode == 0, again.stderr[-2000:]


async def test_an_n_minus_one_writer_records_nothing_and_the_table_stays_empty(
    db: Db, env: Env
) -> None:
    """The previous release's writer has no tracker: it creates the environment, records no row."""
    script_repo(env)
    env.gh.route("PUT", PATH, 200, {})
    scoped = type(env.tenant)(id=uuid4(), github_host=env.tenant.github_host, org="acme")
    await GitHubOutputWriter(env.gateway).apply(scoped, [ENVIRONMENT])
    assert await _rows(db, scoped.id) == []


DELETE = "tb_delete_tenant(uuid, text, bigint)"
DRIFTED = "SELECT prosrc LIKE '%-- drifted%' FROM pg_proc WHERE oid = CAST(:f AS regprocedure)"


async def _drift(db: Db) -> None:
    """Edit the live function the way a hotfix would: one comment inside its body."""
    definition = await _admin_scalar(
        db, "SELECT pg_get_functiondef(CAST(:f AS regprocedure))", {"f": DELETE}
    )
    async with db.admin.begin() as conn:
        await conn.exec_driver_sql(definition.replace("\nBEGIN\n", "\nBEGIN\n    -- drifted\n", 1))


async def test_the_delete_backstop_lists_the_table_and_both_guards_refuse_a_drifted_body() -> None:
    """The new body names `github_environments`; a drifted body is refused up and down."""
    async for scratch in _provision(enforce=False):
        body = await _admin_scalar(
            scratch,
            "SELECT prosrc FROM pg_proc WHERE oid = CAST(:f AS regprocedure)",
            {"f": DELETE},
        )
        assert body.count("'github_environments'") == 2  # before the delete and the re-check
        await _drift(scratch)
        refused = await _alembic(scratch.name, "downgrade", PREVIOUS)
        assert refused.returncode != 0
        assert f"{DELETE} is not the {REVISION} body" in refused.stderr
        assert await _admin_scalar(scratch, DRIFTED, {"f": DELETE})  # nothing was restored
        assert await _admin_scalar(scratch, "SELECT to_regclass('github_environments') IS NOT NULL")

    async for scratch in _provision(enforce=False):
        stepped = await _alembic(scratch.name, "downgrade", PREVIOUS)
        assert stepped.returncode == 0, stepped.stderr[-2000:]
        await _drift(scratch)
        refused = await _alembic(scratch.name, "upgrade", REVISION)
        assert refused.returncode != 0
        assert f"{DELETE} is not the 6f3b9d2e4a85 body" in refused.stderr
        assert await _admin_scalar(scratch, DRIFTED, {"f": DELETE})
        assert await _admin_scalar(scratch, "SELECT to_regclass('github_environments') IS NULL")
