"""The v1 read routes against a real Postgres with row-level security enforced (NG6d-2).

The tenant lists see only their tenant, keyset pages neither repeat nor skip, a delivery never
shows its headers and gives its payload to an operator only, the one-event and one-tenant
definer functions work for the app role, and `/tenants` filters and pages in the database. Runs in
CI's stack; reuses the tenancy suite's scratch database (tenants `a` and `b` on host `tb.test`).
"""

import asyncio
import importlib.util
import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from tests.trustbridge.admin import test_registration_unit as reg
from tests.trustbridge.admin.helpers import build, token
from tests.trustbridge.signer.test_signers import make_key, pem_of
from tests.trustbridge.tenancy import test_rls_integration as rls
from trustbridge.adapters.store import OperationKind
from trustbridge.admin.access import SqlTenantAccess, tenants_page
from trustbridge.audit import Action, ActorType, AuditEvent, AuditWriter, Outcome
from trustbridge.audit.store import get_event
from trustbridge.rbac import Grant, Principal, Role
from trustbridge.tenancy import get_tenant, install_tenant_hook, list_tenants, tenant_context
from trustbridge.tenancy.gate import SqlTenantGate

pytestmark = pytest.mark.integration

HOST = "tb.test"
BASE = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
SECRET_HEADER = "sha256=canary-signature-value"  # noqa: S105 -- a planted canary


@pytest.fixture(scope="module")
async def db() -> AsyncIterator[rls.Db]:
    """The tenancy suite's scratch database with row-level security enforced."""
    async for database in rls._provision(enforce=True):
        yield database


@pytest.fixture(scope="module", autouse=True)
def _hook() -> None:
    install_tenant_hook()


def _client(
    db: rls.Db,
    *roles: str,
    sub: str = "alice",
    scheduler: Any = None,  # noqa: ANN401
    registration: Any = None,  # noqa: ANN401
) -> httpx.AsyncClient:
    env = build(
        access=SqlTenantAccess(db.app),
        factory=db.app,
        writer=AuditWriter(db.app),
        scheduler=scheduler,
        registration=registration,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app, raise_app_exceptions=False),
        base_url="http://admin.test",
        headers=token(sub, *roles),
    )


async def _admin(db: rls.Db, sql: str, **params: object) -> list[Any]:
    async with db.admin.begin() as conn:
        result = await conn.execute(text(sql), params)
        return list(result.all()) if result.returns_rows else []


async def _collect(client: httpx.AsyncClient, url: str, limit: int = 1) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        response = await client.get(url, params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        items.extend(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return items


_SEEDED: set[str] = set()


async def _seed(db: rls.Db) -> None:
    """More rows for tenant `a` (the scratch database already has one of each per tenant), once."""
    if db.name in _SEEDED:
        return
    _SEEDED.add(db.name)
    for number in (2, 3):
        await _admin(
            db,
            "INSERT INTO tenant_repositories (tenant_id, repository_id, full_name) "
            "VALUES (:t, :n, :name)",
            t=db.a,
            n=number,
            name=f"o/r{number}",
        )
    for key in ("k1", "k2"):
        await _admin(
            db,
            "INSERT INTO adapter_resources (tenant_id, adapter, resource_key, state) "
            "VALUES (:t, 'ghcr', :k, CAST('{\"name\": \"n\"}' AS jsonb))",
            t=db.a,
            k=key,
        )
        await _admin(
            db,
            "INSERT INTO adapter_credentials (tenant_id, adapter, scope_key, scope, state) "
            "VALUES (:t, 'ghcr', :k, CAST('{\"permission\": \"pull\"}' AS jsonb), "
            "CAST('{\"current\": null}' AS jsonb))",
            t=db.a,
            k=key,
        )
    for minutes in (1, 2):
        await _admin(
            db,
            "INSERT INTO credential_operations "
            "(id, tenant_id, adapter, scope_key, kind, status, oidc_offered, created_at) "
            "VALUES (gen_random_uuid(), :t, 'ghcr', 'k1', 'issue', 'finished', false, :at)",
            t=db.a,
            at=BASE + timedelta(minutes=minutes),
        )


@pytest.mark.parametrize(
    ("path", "key", "expected_a"),
    [
        ("repositories", "repository_id", [1, 2, 3]),
        ("resources", "resource_key", ["seed", "k1", "k2"]),
        ("credentials", "scope_key", ["seed", "k1", "k2"]),
    ],
)
async def test_tenant_lists_are_scoped_ordered_and_keyset_paged(
    db: rls.Db, path: str, key: str, expected_a: list[Any]
) -> None:
    """Inv. 6/10: only the tenant's rows (RLS), in key order, one page at a time."""
    await _seed(db)
    async with _client(db, f"trustbridge:{HOST}:a:viewer") as mine:
        items = await _collect(mine, f"/admin/v1/tenants/{db.a}/{path}")
        assert [i[key] for i in items] == expected_a
        assert {i["tenant_id"] for i in items} == {str(db.a)}
        assert (await mine.get(f"/admin/v1/tenants/{db.b}/{path}")).status_code == 404
        everything = await _collect(mine, f"/admin/v1/tenants/{db.a}/{path}", limit=200)
        assert [i[key] for i in everything] == expected_a
    async with _client(db, "trustbridge:viewer") as instance:
        theirs = await _collect(instance, f"/admin/v1/tenants/{db.b}/{path}")
        assert {i["tenant_id"] for i in theirs} == {str(db.b)}
        assert len(theirs) == 1


async def test_credentials_and_resources_return_json_objects(db: rls.Db) -> None:
    """The JSONB columns arrive as objects whatever the driver's codec does."""
    await _seed(db)
    async with _client(db, f"trustbridge:{HOST}:a:viewer") as mine:
        listed = (await mine.get(f"/admin/v1/tenants/{db.a}/resources")).json()["items"]
        resource = next(r for r in listed if r["adapter"] == "ghcr")
        assert resource["state"] == {"name": "n", "ref": {}}
        creds = (await mine.get(f"/admin/v1/tenants/{db.a}/credentials")).json()["items"]
        ghcr = next(c for c in creds if c["adapter"] == "ghcr")
        assert ghcr["scope"] == {
            "repository_id": None,
            "repository_owner_id": None,
            "environment": None,
            "permission": "pull",
        }
        assert ghcr["outputs"] == []


async def test_operations_are_newest_first_and_page_without_repeats(db: rls.Db) -> None:
    """The `(created_at, id)` keyset."""
    await _seed(db)
    async with _client(db, f"trustbridge:{HOST}:a:viewer") as mine:
        items = await _collect(mine, f"/admin/v1/tenants/{db.a}/operations")
        times = [i["created_at"] for i in items]
        assert times == sorted(times, reverse=True)
        assert len({i["id"] for i in items}) == len(items) >= 3
        assert {i["tenant_id"] for i in items} == {str(db.a)}


async def _delivery(db: rls.Db, delivery_id: str, minutes: int, body: bytes | None) -> None:
    await _admin(
        db,
        "INSERT INTO webhook_deliveries (host, delivery_id, event, headers, body, received_at) "
        "VALUES (:h, :d, 'push', CAST(:headers AS jsonb), :b, :at)",
        h=HOST,
        d=delivery_id,
        headers=json.dumps({"x-hub-signature-256": SECRET_HEADER}),
        b=body,
        at=BASE + timedelta(minutes=minutes),
    )


async def test_deliveries_never_show_headers_and_give_the_payload_to_operators_only(
    db: rls.Db,
) -> None:
    """The list and the detail: no headers anywhere; the body only for an operator."""
    await _delivery(db, "d1", 1, json.dumps({"action": "opened"}).encode())
    await _delivery(db, "d2", 2, None)
    await _delivery(db, "d3", 3, b"\xff not json")
    async with _client(db, "trustbridge:viewer") as viewer:
        listed = await _collect(viewer, "/admin/v1/deliveries")
        assert [d["delivery_id"] for d in listed][:3] == ["d3", "d2", "d1"]
        assert {d["has_body"] for d in listed if d["delivery_id"] in {"d1", "d2"}} == {True, False}
        one = await viewer.get(f"/admin/v1/deliveries/{HOST}/d1")
        assert one.status_code == 200
        assert one.json()["payload"] is None
        assert SECRET_HEADER not in one.text
        assert "headers" not in one.json()
        assert (await viewer.get(f"/admin/v1/deliveries/{HOST}/nope")).status_code == 404
    async with _client(db, "trustbridge:operator") as operator:
        detail = (await operator.get(f"/admin/v1/deliveries/{HOST}/d1")).json()
        assert detail["payload"] == {"action": "opened"}
        assert SECRET_HEADER not in json.dumps(detail)
        assert (await operator.get(f"/admin/v1/deliveries/{HOST}/d2")).json()["payload"] is None
        assert (await operator.get(f"/admin/v1/deliveries/{HOST}/d3")).json()["payload"] is None
        everything = await operator.get("/admin/v1/deliveries", params={"limit": 200})
        assert SECRET_HEADER not in everything.text
    async with _client(db, f"trustbridge:{HOST}:a:viewer") as tenant_viewer:
        assert (await tenant_viewer.get("/admin/v1/deliveries")).status_code == 403


async def test_one_audit_event_by_id_reads_a_null_tenant_row_through_the_definer_function(
    db: rls.Db,
) -> None:
    """`tb_audit_get` is the only way for the app role to read a null-tenant event by id."""
    writer = AuditWriter(db.app)
    for tenant in (None, db.a):
        await writer.record(
            AuditEvent(
                actor_type=ActorType.SYSTEM,
                actor_sub="worker",
                action=Action.TENANT_SYNC,
                resource_type="tenant",
                outcome=Outcome.SUCCESS,
                tenant_id=tenant,
            )
        )
    rows = await _admin(
        db,
        "SELECT id, tenant_id FROM audit_events WHERE actor_sub = 'worker' "
        "AND action = 'tenant.sync' ORDER BY seq DESC LIMIT 2",
    )
    by_tenant = {row.tenant_id: row.id for row in rows}
    async with _client(db, "trustbridge:auditor") as auditor:
        for tenant, event_id in by_tenant.items():
            body = (await auditor.get(f"/admin/v1/audit-events/{event_id}")).json()
            assert body["id"] == str(event_id)
            assert body["tenant_id"] == (None if tenant is None else str(tenant))
        assert (await auditor.get(f"/admin/v1/audit-events/{uuid4()}")).status_code == 404
    async with _client(db, f"trustbridge:{HOST}:a:auditor") as tenant_auditor:
        assert (
            await tenant_auditor.get(f"/admin/v1/audit-events/{by_tenant[None]}")
        ).status_code == 403
    async with db.app() as session, session.begin():
        assert await get_event(session, uuid4()) is None
        found = await get_event(session, by_tenant[None])
        assert found is not None
        assert found.tenant_id is None
        direct = (
            await session.execute(
                text("SELECT count(*) FROM audit_events WHERE id = :i"), {"i": by_tenant[None]}
            )
        ).scalar_one()
        assert direct == 0  # a plain SELECT cannot see it: that is why the function exists


async def test_the_new_functions_are_definer_functions_granted_to_the_two_roles(
    db: rls.Db,
) -> None:
    """Fixed `search_path`, `SECURITY DEFINER`, `EXECUTE` for the two roles and not PUBLIC."""
    rows = await _admin(
        db,
        "SELECT proname, prosecdef, proconfig FROM pg_proc "
        "WHERE proname IN ('tb_audit_get', 'tb_get_tenant', 'tb_set_tenant_disabled')",
    )
    assert {r.proname for r in rows} == {"tb_audit_get", "tb_get_tenant", "tb_set_tenant_disabled"}
    for row in rows:
        assert row.prosecdef is True
        assert "search_path=pg_catalog, public, pg_temp" in row.proconfig
    probe = "SELECT has_function_privilege(:role, CAST(:fn AS regprocedure), 'EXECUTE')"
    public = (
        "SELECT proacl IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM unnest(proacl) AS a WHERE a::text LIKE '=%') "
        "FROM pg_proc WHERE oid = CAST(:fn AS regprocedure)"
    )
    for function in (
        "tb_audit_get(uuid)",
        "tb_get_tenant(uuid)",
        "tb_set_tenant_disabled(uuid, text, text, boolean)",
    ):
        for role in (rls.APP_ROLE, rls.OWNER_ROLE):
            assert (await _admin(db, probe, role=role, fn=function))[0][0] is True, (function, role)
        assert (await _admin(db, public, fn=function))[0][0] is True, function


async def test_get_tenant_reads_one_row_by_id_and_none_when_absent(db: rls.Db) -> None:
    """`tb_get_tenant` returns the same columns as `tb_list_tenants`."""
    info = await get_tenant(db.app, db.a)
    assert info is not None
    assert (info.id, info.host, info.org, info.state) == (db.a, HOST, "a", "active")
    assert info.admin_disabled_at is None
    assert info.deleting is False
    assert await get_tenant(db.app, uuid4()) is None


async def test_tenants_are_filtered_and_paged_in_the_database(db: rls.Db) -> None:
    """Instance grant: all, by id; organisation grant: only that organisation (any case)."""
    both = sorted([db.a, db.b], key=str)
    instance = Principal("u", None, frozenset({Grant(Role.VIEWER)}))
    rows = await tenants_page(db.app, instance, after=None, limit=10)
    assert [r.id for r in rows] == both
    assert [r.id for r in await tenants_page(db.app, instance, after=both[0], limit=10)] == [
        both[1]
    ]
    assert [r.id for r in await tenants_page(db.app, instance, after=None, limit=1)] == [both[0]]
    mine = Principal("u", None, frozenset({Grant(Role.AUDITOR, HOST, "a")}))
    assert [r.id for r in await tenants_page(db.app, mine, after=None, limit=10)] == [db.a]
    other_host = Principal("u", None, frozenset({Grant(Role.VIEWER, "elsewhere", "a")}))
    assert await tenants_page(db.app, other_host, after=None, limit=10) == []
    async with _client(db, f"trustbridge:{HOST}:a:viewer") as viewer:
        assert [t["id"] for t in await _collect(viewer, "/admin/v1/tenants")] == [str(db.a)]
        assert (await viewer.get(f"/admin/v1/tenants/{db.a}")).json()["org"] == "a"
        me = (await viewer.get("/admin/v1/me")).json()
        assert [t["id"] for t in me["tenants"]] == [str(db.a)]
    async with _client(db, "trustbridge:viewer") as everything:
        assert [t["id"] for t in await _collect(everything, "/admin/v1/tenants")] == [
            str(i) for i in both
        ]
        body = (await everything.get(f"/admin/v1/tenants/{db.b}")).json()
        assert body["admin_disabled_by"] is None
        assert body["admin_disable_reason"] is None
        assert (await everything.get(f"/admin/v1/tenants/{UUID(int=5)}")).status_code == 404


# ---- the admin-disable columns and tb_set_tenant_disabled (NG6e-1) ---------------------------

_SET = text("SELECT * FROM tb_set_tenant_disabled(:id, :by, :reason, :disable)")


async def _set(
    db: rls.Db, tenant: UUID, *, disable: bool | None, by: str = "u-1", reason: str = "hold"
) -> Any:  # noqa: ANN401 -- a result row
    with tenant_context(None):
        async with db.app() as session, session.begin():
            result = await session.execute(
                _SET, {"id": tenant, "by": by, "reason": reason, "disable": disable}
            )
            return result.one()


async def test_disable_is_atomic_keeps_the_first_actor_and_enable_clears_all_three(
    db: rls.Db,
) -> None:
    """Before and after come back for the audit event; a repeat disable changes nothing."""
    try:
        first = await _set(db, db.a, disable=True, by="u-1", reason="billing hold")
        assert first.tenant_found is True
        assert first.before_disabled_at is None
        assert (first.after_disabled_by, first.after_disable_reason) == ("u-1", "billing hold")
        again = await _set(db, db.a, disable=True, by="u-2", reason="other")
        assert again.before_disabled_by == "u-1"
        assert (again.after_disabled_at, again.after_disabled_by, again.after_disable_reason) == (
            first.after_disabled_at,
            "u-1",
            "billing hold",
        )
        info = await get_tenant(db.app, db.a)
        assert info is not None
        assert (info.admin_disabled_at, info.admin_disabled_by, info.admin_disable_reason) == (
            first.after_disabled_at,
            "u-1",
            "billing hold",
        )
        listed = {t.id: t for t in await list_tenants(db.app)}
        assert listed[db.a].admin_disabled_by == "u-1"
        assert listed[db.b].admin_disabled_at is None
        # the installation sync's upsert never touches the columns (ADR 0031 invariant 7)
        with tenant_context(None):
            async with db.app() as session, session.begin():
                await session.execute(
                    text("SELECT tb_upsert_tenant('tb.test', 1, 'a', 1, 'suspended')")
                )
        info = await get_tenant(db.app, db.a)
        assert info is not None
        assert info.admin_disabled_by == "u-1"
        assert info.state == "suspended"
    finally:
        cleared = await _set(db, db.a, disable=False)
        await _admin(db, "UPDATE tenants SET state = 'active' WHERE id = :t", t=db.a)
    assert cleared.before_disabled_by == "u-1"
    assert (cleared.after_disabled_at, cleared.after_disabled_by) == (None, None)
    info = await get_tenant(db.app, db.a)
    assert info is not None
    assert (info.admin_disabled_at, info.admin_disabled_by, info.admin_disable_reason) == (
        None,
        None,
        None,
    )


async def test_an_unknown_tenant_is_not_found_and_a_bad_request_is_refused(db: rls.Db) -> None:
    """No row: `tenant_found` is false. An empty or oversized reason or actor is refused."""
    missing = await _set(db, uuid4(), disable=True)
    assert missing.tenant_found is False
    controls = ({"reason": "a\nb"}, {"reason": "a\x1b[0m"}, {"by": "u\x7f"}, {"by": "u\x85"})
    for kwargs in (
        {"reason": ""},
        {"reason": "x" * 501},
        {"by": ""},
        {"by": "y" * 256},
        *controls,
    ):
        with pytest.raises(DBAPIError, match="invalid disable request"):
            await _set(db, db.b, disable=True, **kwargs)
    info = await get_tenant(db.app, db.b)
    assert info is not None
    assert info.admin_disabled_at is None


async def test_a_null_flag_is_refused_and_enabling_an_enabled_tenant_is_a_no_op(
    db: rls.Db,
) -> None:
    """NULL is neither disable nor enable; an enable of an enabled tenant writes nothing."""
    with pytest.raises(DBAPIError, match="invalid disable request"):
        await _set(db, db.b, disable=None)
    row = await _set(db, db.b, disable=False)
    assert row.tenant_found is True
    assert (row.before_disabled_at, row.after_disabled_at) == (None, None)


async def test_the_columns_are_all_set_or_all_null(db: rls.Db) -> None:
    """The CHECK refuses a partial write, even from the table owner."""
    with pytest.raises(DBAPIError, match="ck_tenants_admin_disabled"):
        await _admin(db, "UPDATE tenants SET admin_disabled_at = now() WHERE id = :t", t=db.b)


def _body(source: str, pattern: str) -> str:
    match = re.search(pattern + r".*?AS \$\$(.*?)\$\$", source, re.DOTALL)
    assert match is not None
    return match.group(1)


def _revision(prefix: str) -> str:
    return next((rls.ROOT / "alembic" / "versions").glob(f"{prefix}_*.py")).read_text()


def _run_ng6e_migration(sync_conn: Connection, direction: str) -> None:
    path = next((rls.ROOT / "alembic" / "versions").glob("b7d3f5a19c68_*.py"))
    spec = importlib.util.spec_from_file_location("ng6e_admin_disable_revision", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(sync_conn)):
        getattr(module, direction)()


async def test_the_downgrade_is_refused_while_a_tenant_is_admin_disabled(db: rls.Db) -> None:
    """Dropping the columns would silently enable the tenant again on the old release."""
    await _set(db, db.b, disable=True)
    try:
        async with db.admin.connect() as conn:
            trans = await conn.begin()
            try:
                with pytest.raises(DBAPIError, match="cannot downgrade"):
                    await conn.run_sync(_run_ng6e_migration, "downgrade")
            finally:
                await trans.rollback()
    finally:
        await _set(db, db.b, disable=False)


async def test_the_downgrade_restores_the_old_function_bodies_exactly(db: rls.Db) -> None:
    """N-1: the previous release's `tb_list_tenants` and `tb_get_tenant`, byte for byte.

    Runs in a transaction that is rolled back, so the module's database is left as it was.
    """
    probe = text(
        "SELECT proname, prosrc, pg_get_function_result(oid) AS result FROM pg_proc "
        "WHERE proname IN ('tb_list_tenants', 'tb_get_tenant', 'tb_set_tenant_disabled')"
    )
    old_list = _body(_revision("d4b8f1e60a27"), r"CREATE OR REPLACE FUNCTION tb_list_tenants\(\)")
    old_get = _body(_revision("a1c4e8d27b53"), r"CREATE OR REPLACE FUNCTION tb_get_tenant")
    async with db.admin.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.run_sync(_run_ng6e_migration, "downgrade")
            rows = {r.proname: r for r in (await conn.execute(probe)).all()}
            assert set(rows) == {"tb_list_tenants", "tb_get_tenant"}
            assert rows["tb_list_tenants"].prosrc == old_list
            assert rows["tb_get_tenant"].prosrc == old_get
            assert "admin_disabled_by" not in rows["tb_list_tenants"].result
            columns = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_name = 'tenants' AND column_name LIKE 'admin_disable%'"
                    )
                )
            ).scalar_one()
            assert columns == 0
            await conn.run_sync(_run_ng6e_migration, "upgrade")
            rows = {r.proname: r for r in (await conn.execute(probe)).all()}
            assert set(rows) == {"tb_list_tenants", "tb_get_tenant", "tb_set_tenant_disabled"}
            assert "admin_disabled_by" in rows["tb_list_tenants"].result
        finally:
            await trans.rollback()


async def test_the_disable_and_enable_routes_write_the_columns_and_the_event_together(
    db: rls.Db,
) -> None:
    """Through the real stack with row-level security: the change and its audit rows."""
    path = f"/admin/v1/tenants/{db.a}"
    secret = "canary-secret-7f3a9c1e"  # noqa: S105 -- a planted canary
    try:
        async with _client(db, "trustbridge:admin", sub="admin-1") as client:
            first = await client.post(f"{path}/disable", json={"reason": f"hold token={secret}"})
            assert first.status_code == 200, first.text
            body = first.json()
            assert body["effective_state"] == "disabled"
            assert body["admin_disabled_by"] == "admin-1"
            assert secret not in first.text
            again = await client.post(f"{path}/disable", json={"reason": "second"})
            assert again.json()["admin_disabled_at"] == body["admin_disabled_at"]
            assert again.json()["admin_disable_reason"] == body["admin_disable_reason"]
            enabled = await client.post(f"{path}/enable")
            assert enabled.json()["admin_disabled_at"] is None
            assert (await client.post(f"{path}/enable")).status_code == 200
        rows = await _admin(
            db,
            "SELECT action, outcome, actor_sub, before, after FROM audit_events "
            "WHERE resource_id = :t AND action IN ('tenant.disable', 'tenant.enable') "
            "ORDER BY seq",
            t=str(db.a),
        )
        assert [(r.action, r.outcome) for r in rows] == [
            ("tenant.disable", "success"),
            ("tenant.disable", "success"),
            ("tenant.enable", "success"),
            ("tenant.enable", "success"),
        ]
        assert {r.actor_sub for r in rows} == {"admin-1"}
        assert rows[1].before == rows[1].after  # the repeat changed nothing
        assert rows[3].before == rows[3].after
        assert rows[0].before["admin_disabled_at"] is None
        assert rows[0].after["admin_disabled_by"] == "admin-1"
        assert secret not in repr([tuple(r) for r in rows])
    finally:
        await _set(db, db.a, disable=False)


async def test_a_tenant_scoped_admin_cannot_disable_through_the_stack(db: rls.Db) -> None:
    """403 for an `admin` on the tenant itself, 404 on another tenant; nothing changes."""
    async with _client(db, "trustbridge:tb.test:a:admin") as client:
        assert (
            await client.post(f"/admin/v1/tenants/{db.a}/disable", json={"reason": "x"})
        ).status_code == 403
        assert (
            await client.post(f"/admin/v1/tenants/{db.b}/disable", json={"reason": "x"})
        ).status_code == 404
    info = await get_tenant(db.app, db.a)
    assert info is not None
    assert info.admin_disabled_at is None


async def test_the_sql_tenant_gate_follows_the_disable_flag(db: rls.Db) -> None:
    """By installation for the worker, by tenant id for the adapter core."""
    gate = SqlTenantGate(db.app)
    try:
        assert await gate.disabled(HOST, 1) is False
        await _set(db, db.a, disable=True)
        assert await gate.disabled(HOST, 1) is True
        assert await gate.disabled(HOST, 2) is False
        assert await gate.disabled(HOST, 999_999) is False
        assert await gate.allows(db.a, OperationKind.ISSUE) is False
        assert await gate.allows(db.b, None) is True
    finally:
        await _set(db, db.a, disable=False)
    assert await gate.disabled(HOST, 1) is False
    assert await gate.allows(db.a, None) is True


# ---- delivery requeue and tenant reconcile (NG6e-3) ----------------------------------------


async def _set_status(
    db: rls.Db, delivery_id: str, status: str, *, body: bool = True, error: str | None = None
) -> None:
    await _admin(
        db,
        "UPDATE webhook_deliveries SET status = CAST(:s AS text), attempts = 8, last_error = :e, "
        "dead_at = CASE WHEN CAST(:s AS text) = 'dead' THEN now() END, "
        "body = CASE WHEN CAST(:keep AS boolean) THEN body END "
        "WHERE host = :h AND delivery_id = :d",
        s=status,
        e=error,
        keep=body,
        h=HOST,
        d=delivery_id,
    )


async def test_requeue_resets_a_dead_delivery_and_audits_it_in_the_same_transaction(
    db: rls.Db,
) -> None:
    """Dead with a body: pending again, no attempts, no error, one event; headers never shown."""
    await _delivery(db, "rq1", 1, json.dumps({"action": "opened"}).encode())
    await _set_status(db, "rq1", "dead", error="boom authorization: Bearer canary-secret-7f3a9c1e")
    async with _client(db, "trustbridge:operator", sub="op-1") as client:
        response = await client.post(f"/admin/v1/deliveries/{HOST}/rq1/requeue")
        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["status"], body["attempts"], body["dead_at"]) == ("pending", 0, None)
        assert body["payload"] == {"action": "opened"}
        assert SECRET_HEADER not in response.text
        again = await client.post(f"/admin/v1/deliveries/{HOST}/rq1/requeue")
        assert again.status_code == 200  # pending: a no-op
    row = (
        await _admin(
            db,
            "SELECT status, attempts, last_error FROM webhook_deliveries "
            "WHERE host = :h AND delivery_id = 'rq1'",
            h=HOST,
        )
    )[0]
    assert tuple(row) == ("pending", 0, None)
    events = await _admin(
        db,
        "SELECT action, outcome, actor_sub, tenant_id, resource_id, before, after "
        "FROM audit_events WHERE resource_id = :r ORDER BY seq",
        r=f"{HOST}/rq1",
    )
    assert [(e.action, e.outcome, e.actor_sub, e.tenant_id) for e in events] == [
        ("delivery.requeue", "success", "op-1", None)
    ] * 2
    assert events[0].before["status"] == "dead"
    assert events[0].after["status"] == "pending"
    assert "canary-secret-7f3a9c1e" not in repr([tuple(e) for e in events])
    assert events[1].before == events[1].after


async def test_requeue_refuses_a_processed_delivery_and_one_without_a_body(db: rls.Db) -> None:
    """409 and 404 change nothing and leave a failure event each; a viewer is a 403."""
    await _delivery(db, "rq2", 2, b"{}")
    await _set_status(db, "rq2", "processed")
    await _delivery(db, "rq3", 3, b"{}")
    await _set_status(db, "rq3", "dead", body=False)
    async with _client(db, "trustbridge:operator") as client:
        for name in ("rq2", "rq3"):
            refused = await client.post(f"/admin/v1/deliveries/{HOST}/{name}/requeue")
            assert refused.status_code == 409
        assert (await client.post(f"/admin/v1/deliveries/{HOST}/none/requeue")).status_code == 404
    async with _client(db, "trustbridge:viewer") as client:
        assert (await client.post(f"/admin/v1/deliveries/{HOST}/rq3/requeue")).status_code == 403
    states = await _admin(
        db,
        "SELECT delivery_id, status FROM webhook_deliveries WHERE delivery_id IN ('rq2', 'rq3') "
        "ORDER BY delivery_id",
    )
    assert [tuple(r) for r in states] == [("rq2", "processed"), ("rq3", "dead")]
    events = await _admin(
        db,
        "SELECT resource_id, outcome, tenant_id, after FROM audit_events "
        "WHERE action = 'delivery.requeue' AND resource_id IN (:a, :b, :c) ORDER BY seq",
        a=f"{HOST}/rq2",
        b=f"{HOST}/rq3",
        c=f"{HOST}/none",
    )
    # one failure event per refusal (the viewer's 403 is the guard's `authz.denied`, not this)
    assert [(e.resource_id, e.outcome, e.tenant_id, e.after) for e in events] == [
        (f"{HOST}/rq2", "failure", None, {"reason": "conflict"}),
        (f"{HOST}/rq3", "failure", None, {"reason": "conflict"}),
        (f"{HOST}/none", "failure", None, {"reason": "not_found"}),
    ]


class Scheduler:
    """A `ReconcileScheduler` that records."""

    def __init__(self) -> None:
        """Start empty."""
        self.tenants: list[UUID] = []

    async def schedule(self, tenant_id: UUID) -> None:
        """Record the request."""
        self.tenants.append(tenant_id)


async def test_reconcile_records_the_request_and_audits_it_for_a_tenant_operator(
    db: rls.Db,
) -> None:
    """A tenant-scoped operator may ask; the event carries the tenant; a disabled tenant is 409."""
    scheduler = Scheduler()
    path = f"/admin/v1/tenants/{db.a}/reconcile"
    try:
        async with _client(db, "trustbridge:tb.test:a:operator", scheduler=scheduler) as client:
            response = await client.post(path)
            assert (response.status_code, response.json()) == (202, {"requested": True})
            assert (await client.post(path)).status_code == 202
            other = await client.post(f"/admin/v1/tenants/{db.b}/reconcile")
            assert other.status_code == 404
        assert scheduler.tenants == [db.a, db.a]
        events = await _admin(
            db,
            "SELECT outcome, tenant_id, before, after FROM audit_events "
            "WHERE action = 'tenant.reconcile' AND resource_id = :t",
            t=str(db.a),
        )
        assert [(e.outcome, e.tenant_id, e.before, e.after) for e in events] == [
            ("success", db.a, None, None)
        ] * 2
        await _set(db, db.a, disable=True)
        async with _client(db, "trustbridge:operator", scheduler=scheduler) as client:
            assert (await client.post(path)).status_code == 409
        assert scheduler.tenants == [db.a, db.a]
        failed = await _admin(
            db,
            "SELECT tenant_id, after FROM audit_events WHERE action = 'tenant.reconcile' "
            "AND outcome = 'failure' AND resource_id = :t",
            t=str(db.a),
        )
        assert [(e.tenant_id, e.after) for e in failed] == [(db.a, {"reason": "tenant_disabled"})]
    finally:
        await _set(db, db.a, disable=False)


# ---- host registration (NG6e-4) -------------------------------------------------------------


async def test_registration_end_to_end_stores_the_app_audits_it_and_leaks_nothing(
    db: rls.Db, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Start, complete, then every re-registration is a 409; the rows hold ids and a reference."""
    caplog.set_level(logging.DEBUG)
    world = reg.World(tmp_path)
    runtime = world.runtime()
    body = {"app_id": 9, "pem": reg.PEM, "webhook_secret": reg.WEBHOOK_SECRET}
    try:
        async with _client(db, "trustbridge:admin", sub="admin-1", registration=runtime) as client:
            started = await client.post(reg.START)
            assert started.status_code == 200, started.text
            done = await client.post(
                reg.COMPLETE, json={"code": reg.CODE, "state": started.json()["state"]}
            )
            assert done.status_code == 200, done.text
            assert done.json()["app_id"] == 123
            assert (await client.post(reg.START)).status_code == 409
            assert (await client.post(reg.MANUAL, json=body)).status_code == 409
        stored = await _admin(
            db, "SELECT app_id, webhook_secret_ref, app_slug FROM github_host_registrations"
        )
        assert [tuple(r) for r in stored] == [
            (123, "store:github-hosts/github.com/webhook-secret", "trustbridge-github-com")
        ]
        events = await _admin(
            db,
            "SELECT action, outcome, actor_sub, tenant_id, before, after FROM audit_events "
            "WHERE resource_type = 'host' AND resource_id = 'github.com' ORDER BY seq",
        )
        assert [(e.action, e.outcome, e.actor_sub, e.tenant_id) for e in events] == [
            ("host.registration.start", "success", "admin-1", None),
            ("host.registration.complete", "success", "admin-1", None),
            ("host.registration.start", "failure", "admin-1", None),
            ("host.registration.manual", "failure", "admin-1", None),
        ]
        assert events[1].after["app_id"] == 123
        haystack = repr([tuple(e) for e in events] + [tuple(r) for r in stored]) + caplog.text
        for canary in (reg.PEM, "BEGIN", reg.WEBHOOK_SECRET, reg.CODE):
            assert canary not in haystack
    finally:
        await _admin(db, "DELETE FROM github_host_registrations")


async def test_two_registrations_of_one_host_cannot_overwrite_each_others_key_or_secret(
    db: rls.Db, tmp_path: Path
) -> None:
    """The claim is a Postgres lock: the loser is a 409 before it writes anything."""
    world = reg.World(tmp_path)
    world.secrets.delay = 0.4  # the winner is still writing when the other arrives
    runtime = world.runtime()
    keys = {1: (pem_of(make_key()), "secret-one"), 2: (pem_of(make_key()), "secret-two")}

    async def register(app_id: int) -> int:
        pem, secret = keys[app_id]
        body = {"app_id": app_id, "pem": pem, "webhook_secret": secret}
        async with _client(db, "trustbridge:admin", registration=runtime) as client:
            return (await client.post(reg.MANUAL, json=body)).status_code

    try:
        statuses = await asyncio.gather(register(1), register(2))
        assert sorted(statuses) == [200, 409]
        winner = 1 if statuses[0] == 200 else 2
        pem, secret = keys[winner]
        assert world.signer_path.read_text() == pem
        assert list(world.secrets.items.values()) == [secret]
        stored = await _admin(db, "SELECT app_id FROM github_host_registrations")
        assert [r.app_id for r in stored] == [winner]
    finally:
        await _admin(db, "DELETE FROM github_host_registrations")
