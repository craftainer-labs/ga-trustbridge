"""The v1 read routes against a real Postgres with row-level security enforced (NG6d-2).

The tenant lists see only their tenant, keyset pages neither repeat nor skip, a delivery never
shows its headers and gives its payload to an operator only, the one-event and one-tenant
definer functions work for the app role, and `/tenants` filters and pages in the database. Runs in
CI's stack; reuses the tenancy suite's scratch database (tenants `a` and `b` on host `tb.test`).
"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from tests.trustbridge.admin.helpers import build, token
from tests.trustbridge.tenancy import test_rls_integration as rls
from trustbridge.admin.access import SqlTenantAccess, tenants_page
from trustbridge.audit import Action, ActorType, AuditEvent, AuditWriter, Outcome
from trustbridge.audit.store import get_event
from trustbridge.rbac import Grant, Principal, Role
from trustbridge.tenancy import get_tenant, install_tenant_hook

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


def _client(db: rls.Db, *roles: str, sub: str = "alice") -> httpx.AsyncClient:
    env = build(access=SqlTenantAccess(db.app), factory=db.app, writer=AuditWriter(db.app))
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
        "WHERE proname IN ('tb_audit_get', 'tb_get_tenant')",
    )
    assert {r.proname for r in rows} == {"tb_audit_get", "tb_get_tenant"}
    for row in rows:
        assert row.prosecdef is True
        assert "search_path=pg_catalog, public" in row.proconfig
    probe = "SELECT has_function_privilege(:role, CAST(:fn AS regprocedure), 'EXECUTE')"
    public = (
        "SELECT proacl IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM unnest(proacl) AS a WHERE a::text LIKE '=%') "
        "FROM pg_proc WHERE oid = CAST(:fn AS regprocedure)"
    )
    for function in ("tb_audit_get(uuid)", "tb_get_tenant(uuid)"):
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
