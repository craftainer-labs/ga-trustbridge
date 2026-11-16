"""The tenant-deletion routes and `tb_skip_tenant_deletion_step` against a real Postgres.

NG6h-7b: the skip function's outcomes (migration `2c8e5b1f7d34`, called as `trustbridge_app`) and
the routes end to end with row-level security enforced: the record outside RLS, every audit event
inside the deletion's tenant context. Runs in CI's stack (integration), never in the unit lane.
It has a scratch database of its own, with the deletion functions owned by the owner role.
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import Row, text
from sqlalchemy.exc import DBAPIError

from tests.trustbridge.admin.helpers import build, token
from tests.trustbridge.tenancy import test_rls_integration as rls
from tests.trustbridge.tenancy.test_deletion_db_integration import NEW_FUNCTIONS, _alembic
from trustbridge.admin.access import SqlTenantAccess
from trustbridge.audit import AuditDenials, AuditWriter
from trustbridge.tenancy import install_tenant_hook

pytestmark = pytest.mark.integration

HOST = "tb.test"
REVISION = "2c8e5b1f7d34"
PREVIOUS = "9a4d6c2e8b17"
SKIP_FUNCTION = "tb_skip_tenant_deletion_step(uuid, text)"
SKIP = "SELECT * FROM tb_skip_tenant_deletion_step(:d, :a)"


@pytest.fixture(scope="module")
async def db() -> AsyncIterator[rls.Db]:
    """A scratch database with RLS enforced and the deletion functions owned by the owner."""
    async for provisioned in rls._provision(enforce=True):
        async with provisioned.admin.begin() as conn:
            for function in (*NEW_FUNCTIONS, SKIP_FUNCTION):
                await conn.execute(text(f"ALTER FUNCTION {function} OWNER TO {rls.OWNER_ROLE}"))
        yield provisioned


@pytest.fixture(scope="module", autouse=True)
def _hook() -> None:
    install_tenant_hook()


async def _admin(db: rls.Db, sql: str, **params: Any) -> list[Row[Any]]:  # noqa: ANN401
    async with db.admin.begin() as conn:
        result = await conn.execute(text(sql), params)
        return list(result.all()) if result.returns_rows else []


async def _app(db: rls.Db, sql: str, **params: Any) -> list[Row[Any]]:  # noqa: ANN401
    async with db.app() as session, session.begin():
        return list((await session.execute(text(sql), params)).all())


async def _open(db: rls.Db, tenant: UUID, state: str = "requested", **columns: Any) -> UUID:  # noqa: ANN401
    """A deletion record of `tenant` in `state`, made as the owner would (the tenant disabled)."""
    await _admin(
        db,
        "UPDATE tenants SET admin_disabled_at = now(), admin_disabled_by = 'a', "
        "admin_disable_reason = 'hold' WHERE id = :t AND admin_disabled_at IS NULL",
        t=tenant,
    )
    rows = await _admin(
        db,
        "INSERT INTO tenant_deletions (tenant_id, host, installation_id, requested_by_sub, "
        "requested_by_iss, state, attempts, last_error, holder, expires_at) "
        "SELECT id, host, installation_id, 'u', 'https://idp.example', CAST(:s AS text), "
        "CAST(:n AS bigint), CAST(:e AS text), CAST(:h AS text), CAST(:x AS timestamptz) "
        "FROM tenants WHERE id = :t RETURNING id",
        t=tenant,
        s=state,
        n=columns.get("attempts", 0),
        e=columns.get("last_error"),
        h=columns.get("holder"),
        x=columns.get("expires_at"),
    )
    return rows[0].id  # type: ignore[no-any-return]


async def _close(db: rls.Db) -> None:
    """Make every open deletion terminal so the one-open-per-tenant index never gets in the way."""
    await _admin(
        db,
        "UPDATE tenant_deletions SET state = 'cancelled', holder = NULL, expires_at = NULL "
        "WHERE state NOT IN ('done', 'failed', 'cancelled')",
    )


async def _record(db: rls.Db, deletion: UUID) -> Row[Any]:
    return (await _admin(db, "SELECT * FROM tenant_deletions WHERE id = :d", d=deletion))[0]


# ---- the function --------------------------------------------------------------------------


async def test_the_app_role_may_execute_the_function_and_public_may_not(db: rls.Db) -> None:
    """Granted to the one runtime role, revoked from PUBLIC, `pg_temp` last in its search path."""
    row = (
        await _admin(
            db,
            "SELECT has_function_privilege('trustbridge_app', "
            "'tb_skip_tenant_deletion_step(uuid, text)', 'EXECUTE') AS app, "
            "has_function_privilege('public', "
            "'tb_skip_tenant_deletion_step(uuid, text)', 'EXECUTE') AS pub, "
            "p.prosecdef AS definer, p.proconfig AS config FROM pg_proc p "
            "WHERE p.oid = 'tb_skip_tenant_deletion_step(uuid, text)'::regprocedure",
        )
    )[0]
    assert (row.app, row.pub, row.definer) == (True, False, True)
    assert list(row.config) == ["search_path=pg_catalog, public, pg_temp"]


async def test_a_skip_refuses_what_it_must_and_changes_nothing(db: rls.Db) -> None:
    """Unknown, ended, not skippable, not failed and leased records are refused."""
    await _close(db)
    unknown = await _app(db, SKIP, d=UUID(int=0), a="admin-1")
    assert tuple(unknown[0]) == ("not_found", None, None)
    for state in ("done", "failed", "cancelled"):
        deletion = await _open(db, db.b, state)
        assert tuple((await _app(db, SKIP, d=deletion, a="admin-1"))[0]) == (
            "terminal",
            state,
            None,
        )
    for state in ("requested", "draining"):
        await _close(db)
        deletion = await _open(db, db.a, state, attempts=3, last_error="boom")
        assert tuple((await _app(db, SKIP, d=deletion, a="admin-1"))[0]) == (
            "not_skippable",
            state,
            None,
        )
    await _close(db)
    failing = await _open(db, db.a, "platform", attempts=0)
    assert tuple((await _app(db, SKIP, d=failing, a="admin-1"))[0]) == (
        "not_failed",
        "platform",
        None,
    )
    await _admin(
        db,
        "UPDATE tenant_deletions SET attempts = 2, last_error = 'x', holder = 'w1', "
        "expires_at = now() + interval '1 hour' WHERE id = :d",
        d=failing,
    )
    assert tuple((await _app(db, SKIP, d=failing, a="admin-1"))[0]) == (
        "lease_held",
        "platform",
        None,
    )
    record = await _record(db, failing)
    assert (record.state, record.attempts, record.holder, record.fence) == ("platform", 2, "w1", 0)
    assert record.skipped == []


@pytest.mark.parametrize("actor", [None, ""])
async def test_a_skip_needs_an_actor(db: rls.Db, actor: str | None) -> None:
    """The acknowledgement names who; an empty actor is an invalid argument."""
    with pytest.raises(DBAPIError, match="an actor is required"):
        await _app(db, SKIP, d=UUID(int=0), a=actor)


@pytest.mark.parametrize(
    ("state", "to_state", "cursor"),
    [("platform", "github", {}), ("github", "database", {}), ("database", "database", None)],
)
async def test_a_skip_acknowledges_the_step_and_starts_the_next_one_clean(
    db: rls.Db, state: str, to_state: str, cursor: dict[str, Any] | None
) -> None:
    """One `skipped` entry (who, when, no free text), a clean retry state and a new fence."""
    await _close(db)
    deletion = await _open(
        db, db.a, state, attempts=4, last_error="Boom: private details", holder="old"
    )
    await _admin(
        db,
        "UPDATE tenant_deletions SET expires_at = now() - interval '1 second', retry_at = now() "
        "+ interval '1 hour', fence = 5, step_cursor = CAST(:c AS jsonb) WHERE id = :d",
        d=deletion,
        c='{"table": "x"}',
    )
    result = await _app(db, SKIP, d=deletion, a="admin-9")
    assert tuple(result[0]) == ("skipped", state, to_state)
    record = await _record(db, deletion)
    assert (record.state, record.attempts, record.last_error, record.retry_at) == (
        to_state,
        0,
        None,
        None,
    )
    assert (record.holder, record.expires_at, record.fence) == (None, None, 6)
    assert record.step_cursor == (cursor if cursor is not None else {"operator_skipped": True})
    (entry,) = record.skipped
    assert {k: v for k, v in entry.items() if k != "acknowledged_at"} == {
        "category": "step",
        "reason": "operator_skipped",
        "step": state,
        "acknowledged_by": "admin-9",
    }
    assert entry["acknowledged_at"].endswith("Z")
    assert "private" not in str(record.skipped)


async def test_a_stale_holder_cannot_commit_after_a_skip(db: rls.Db) -> None:
    """The fence moved: the old lease's holder is refused with `TB003`."""
    await _close(db)
    deletion = await _open(db, db.a, "platform", attempts=1, last_error="x", holder="old")
    await _admin(
        db,
        "UPDATE tenant_deletions SET expires_at = now() - interval '1 second', fence = 1 "
        "WHERE id = :d",
        d=deletion,
    )
    await _app(db, SKIP, d=deletion, a="admin-9")
    with pytest.raises(DBAPIError, match="deletion lease lost"):
        await _app(db, "SELECT tb_renew_tenant_deletion(:d, 'old', 1, 60)", d=deletion)


async def test_a_second_skip_needs_a_new_failure(db: rls.Db) -> None:
    """Attempts were reset, so the next step is `not_failed` until it fails itself."""
    await _close(db)
    deletion = await _open(db, db.a, "platform", attempts=1, last_error="x")
    assert (await _app(db, SKIP, d=deletion, a="a1"))[0].outcome == "skipped"
    assert (await _app(db, SKIP, d=deletion, a="a1"))[0].outcome == "not_failed"
    await _admin(db, "UPDATE tenant_deletions SET attempts = 1 WHERE id = :d", d=deletion)
    assert (await _app(db, SKIP, d=deletion, a="a1"))[0].outcome == "skipped"
    assert (await _record(db, deletion)).state == "database"
    await _admin(db, "UPDATE tenant_deletions SET attempts = 1 WHERE id = :d", d=deletion)
    assert (await _app(db, SKIP, d=deletion, a="a1"))[0].to_state == "database"  # stays


async def test_the_migration_downgrades_and_upgrades_again() -> None:
    """The downgrade drops the function and touches no row; the upgrade brings it back."""
    async for scratch in rls._provision(enforce=False):
        down = await _alembic(scratch.name, "downgrade", PREVIOUS)
        assert down.returncode == 0, down.stderr[-2000:]
        probe = "SELECT to_regprocedure('tb_skip_tenant_deletion_step(uuid, text)') IS NULL AS gone"
        assert (await _admin(scratch, probe))[0].gone
        up = await _alembic(scratch.name, "upgrade", REVISION)
        assert up.returncode == 0, up.stderr[-2000:]
        assert not (await _admin(scratch, probe))[0].gone


# ---- the routes under row-level security ---------------------------------------------------


def _client(db: rls.Db, *roles: str) -> httpx.AsyncClient:
    writer = AuditWriter(db.app)
    env = build(
        access=SqlTenantAccess(db.app),
        denials=AuditDenials(writer),
        factory=db.app,
        writer=writer,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app, raise_app_exceptions=False),
        base_url="http://admin.test",
        headers=token("alice", *roles),
    )


async def _events(db: rls.Db, tenant: UUID) -> list[tuple[str, str]]:
    rows = await _admin(
        db,
        "SELECT action, outcome FROM audit_events WHERE tenant_id = :t AND action IN ("
        "'tenant.deletion.requested', 'tenant.deletion.cancelled', "
        "'tenant.deletion.step_skipped', 'tenant.deletion.operator_failed') ORDER BY seq",
        t=tenant,
    )
    return [(r.action, r.outcome) for r in rows]


async def test_the_whole_flow_runs_under_rls_and_every_event_lands_in_the_tenant(
    db: rls.Db,
) -> None:
    """Request, repeat, mismatch, read, cancel, a new request, skip, fail: rows and events."""
    await _close(db)
    await _admin(
        db,
        "UPDATE tenants SET admin_disabled_at = NULL, admin_disabled_by = NULL, "
        "admin_disable_reason = NULL WHERE id = :t",
        t=db.b,
    )
    body = {"host": HOST, "installation_id": 2, "organization": "B", "reason": "customer asked"}
    async with _client(db, "trustbridge:admin") as admin, _client(db, "trustbridge:viewer") as view:
        url = f"/admin/v1/tenants/{db.b}/deletion"
        # not disabled yet
        assert (await admin.post(url, json=body)).status_code == 409
        assert (
            await admin.post(f"/admin/v1/tenants/{db.b}/disable", json={"reason": "x"})
        ).is_success
        wrong = await admin.post(url, json={**body, "organization": "other"})
        assert wrong.status_code == 409
        first = await admin.post(url, json=body)  # the login is compared without case
        assert first.status_code == 202, first.text
        deletion = first.json()
        assert deletion["state"] == "requested"
        assert deletion["not_before"] is None
        assert "holder" not in deletion
        again = await admin.post(url, json=body)
        assert again.json()["id"] == deletion["id"]
        listed = (await view.get("/admin/v1/tenant-deletions")).json()["items"]
        assert listed[0]["id"] == deletion["id"]
        one = await view.get(f"/admin/v1/tenant-deletions/{deletion['id']}")
        assert one.json() == deletion
        assert (
            await view.post(f"/admin/v1/tenant-deletions/{deletion['id']}/cancel")
        ).status_code == 403
        cancelled = await admin.post(f"/admin/v1/tenant-deletions/{deletion['id']}/cancel")
        assert (cancelled.status_code, cancelled.json()["state"]) == (200, "cancelled")
        assert (
            await admin.post(f"/admin/v1/tenant-deletions/{deletion['id']}/cancel")
        ).status_code == 200
        # a cancelled record is terminal: a new request makes a new one
        second = (await admin.post(url, json=body)).json()
        assert second["id"] != deletion["id"]
        assert (
            await admin.post(
                f"/admin/v1/tenant-deletions/{second['id']}/skip-step", json={"reason": "x"}
            )
        ).status_code == 409  # still `requested`
        await _admin(
            db,
            "UPDATE tenant_deletions SET state = 'platform', attempts = 2, last_error = 'E: x' "
            "WHERE id = :d",
            d=UUID(second["id"]),
        )
        skipped = await admin.post(
            f"/admin/v1/tenant-deletions/{second['id']}/skip-step", json={"reason": "harbor gone"}
        )
        assert skipped.status_code == 200, skipped.text
        shown = skipped.json()
        assert (shown["state"], shown["attempts"], shown["last_error_code"]) == ("github", 0, None)
        assert shown["skipped"][0]["acknowledged_by"] == "alice"
        failed = await admin.post(
            f"/admin/v1/tenant-deletions/{second['id']}/fail", json={"reason": "giving up"}
        )
        assert failed.status_code == 200, failed.text
        assert (failed.json()["state"], failed.json()["last_error_code"]) == (
            "failed",
            "operator_failed",
        )
        assert (
            await admin.post(
                f"/admin/v1/tenant-deletions/{second['id']}/fail", json={"reason": "x"}
            )
        ).status_code == 409  # already ended
        leased = await _open(db, db.a, "platform", attempts=1, last_error="x", holder="w1")
        await _admin(
            db,
            "UPDATE tenant_deletions SET expires_at = now() + interval '1 hour' WHERE id = :d",
            d=leased,
        )
        assert (
            await admin.post(f"/admin/v1/tenant-deletions/{leased}/fail", json={"reason": "x"})
        ).status_code == 409
        assert (
            await admin.post(f"/admin/v1/tenant-deletions/{UUID(int=0)}/cancel")
        ).status_code == 404
    events = await _events(db, db.b)
    assert events == [
        ("tenant.deletion.requested", "failure"),  # not disabled, then the mismatch
        ("tenant.deletion.requested", "failure"),
        ("tenant.deletion.requested", "success"),
        ("tenant.deletion.requested", "success"),  # the repeat
        ("tenant.deletion.cancelled", "success"),
        ("tenant.deletion.cancelled", "success"),
        ("tenant.deletion.requested", "success"),
        ("tenant.deletion.step_skipped", "failure"),
        ("tenant.deletion.step_skipped", "success"),
        ("tenant.deletion.operator_failed", "success"),
        ("tenant.deletion.operator_failed", "failure"),
    ]
    assert await _events(db, db.a) == [("tenant.deletion.operator_failed", "failure")]
    unknown = await _admin(
        db,
        "SELECT count(*) AS n FROM audit_events WHERE resource_id = :i AND tenant_id IS NULL",
        i=str(UUID(int=0)),
    )
    assert unknown[0].n == 1
