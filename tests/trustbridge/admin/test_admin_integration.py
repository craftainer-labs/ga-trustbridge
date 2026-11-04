"""The admin read routes against a real Postgres with row-level security enforced.

ADR 0033 invariant 6 (the tenant context set by an async-generator dependency reaches the
handler's session), ADR 0032 invariants 5, 7, 11, 13 and 14 for the read side: a denial is a
row with the sub, a tenant auditor sees only its tenant, the export is paged, ordered, lag-bounded
and writes its own event. Runs in CI's stack (integration); reuses the tenancy suite's scratch
database fixture (tenants `a` and `b` on host `tb.test`, each with one repository row).
"""

from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import text

from app.oidc import get_current_claims
from app.problem_details import register_problem_handlers
from tests.trustbridge.admin.helpers import _claims, build, token
from tests.trustbridge.tenancy import test_rls_integration as rls
from trustbridge.admin.access import SqlTenantAccess, all_tenants
from trustbridge.audit import Action, ActorType, AuditDenials, AuditEvent, AuditWriter, Outcome
from trustbridge.rbac import Authorized, RbacConfig, RbacRuntime, Role, require_role
from trustbridge.tenancy import current_tenant, install_tenant_hook

pytestmark = pytest.mark.integration

HOST = "tb.test"


@pytest.fixture(scope="module")
async def db() -> AsyncIterator[rls.Db]:
    """The tenancy suite's scratch database with row-level security enforced."""
    async for database in rls._provision(enforce=True):
        yield database


@pytest.fixture(scope="module", autouse=True)
def _hook() -> None:
    """The process installs the tenant hook once, as `main.install` does."""
    install_tenant_hook()


def _client(app: FastAPI, *roles: str, sub: str = "alice") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://admin.test",
        headers=token(sub, *roles),
    )


async def test_the_tenant_context_set_by_the_async_generator_dependency_reaches_the_handler(
    db: rls.Db,
) -> None:  # ADR 0033 inv. 6, the NG6b follow-up
    """The dependency holds `tenant_context` across `yield`; the handler sees only that tenant."""
    app = FastAPI()
    register_problem_handlers(app)
    app.dependency_overrides[get_current_claims] = _claims
    app.state.rbac = RbacRuntime(RbacConfig("api"), SqlTenantAccess(db.app))

    @app.get("/t/{tenant_id}/repositories")
    async def repositories(
        tenant_id: UUID,
        auth: Annotated[Authorized, Depends(require_role(Role.VIEWER, tenant_param="tenant_id"))],
    ) -> dict[str, Any]:
        async with db.app() as session:
            rows = (
                await session.execute(text("SELECT tenant_id FROM tenant_repositories"))
            ).scalars()
            seen = sorted(str(row) for row in rows)
        return {"seen": seen, "context": str(current_tenant.get()), "auth": str(auth.tenant_id)}

    @app.get("/t/{tenant_id}/boom")
    async def boom(
        tenant_id: UUID,
        auth: Annotated[Authorized, Depends(require_role(Role.VIEWER, tenant_param="tenant_id"))],
    ) -> None:
        raise RuntimeError("handler failed")

    async with _client(app, f"trustbridge:{HOST}:a:viewer") as mine:
        own = (await mine.get(f"/t/{db.a}/repositories")).json()
        assert own == {"seen": [str(db.a)], "context": str(db.a), "auth": str(db.a)}
        assert current_tenant.get() is None  # reset when the dependency's `with` ended
        assert (await mine.get(f"/t/{db.b}/repositories")).status_code == 404  # b: no grant
        assert (await mine.get(f"/t/{db.b}/boom")).status_code == 404
        assert (await mine.get(f"/t/{db.a}/boom")).status_code == 500
        assert current_tenant.get() is None  # also when the handler raised
    async with _client(
        app, "trustbridge:viewer"
    ) as instance:  # an instance grant: context per request
        again = (await instance.get(f"/t/{db.b}/repositories")).json()
        assert again["seen"] == [str(db.b)]
        assert (await instance.get(f"/t/{db.a}/repositories")).json()["seen"] == [str(db.a)]


def _app(db: rls.Db) -> FastAPI:
    writer = AuditWriter(db.app)
    env = build(
        access=SqlTenantAccess(db.app),
        denials=AuditDenials(writer),
        factory=db.app,
        writer=writer,
        export_page_size=2,
    )
    env.runtime.__class__  # noqa: B018 -- the runtime is wired by `build`
    return env.app


async def _seed(db: rls.Db) -> None:
    writer = AuditWriter(db.app)
    for tenant in (db.a, db.a, db.b, None, None):
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


async def _rows(db: rls.Db, sql: str, **params: object) -> list[Any]:
    async with db.admin.begin() as conn:
        return list((await conn.execute(text(sql), params)).all())


async def _collect(client: httpx.AsyncClient, url: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        body = (await client.get(url, params=params)).json()
        items.extend(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return items


async def test_denials_are_rows_and_audit_reads_are_scoped_by_grant(db: rls.Db) -> None:
    """Inv. 5/7/11 (ADR 0032) and 3/5 (ADR 0033): a denial is a null-tenant row naming the sub."""
    await _seed(db)
    app = _app(db)
    async with _client(app, f"trustbridge:{HOST}:a:viewer", sub="alice") as viewer:
        assert (await viewer.get("/admin/v1/audit-events")).status_code == 403
        assert (await viewer.get(f"/admin/v1/tenants/{db.a}/audit-events")).status_code == 403
        assert (await viewer.get(f"/admin/v1/tenants/{db.b}/audit-events")).status_code == 404
    rows = await _rows(
        db,
        "SELECT tenant_id, resource_type, resource_id, outcome FROM audit_events "
        "WHERE action = 'authz.denied' AND actor_sub = 'alice' ORDER BY seq",
    )
    assert [tuple(r) for r in rows] == [
        (None, "role", "auditor", "denied"),
        (None, "tenant", str(db.a), "denied"),
        (None, "tenant", str(db.b), "denied"),
    ]
    async with _client(app, f"trustbridge:{HOST}:a:auditor", sub="bob") as tenant_auditor:
        mine = await _collect(tenant_auditor, f"/admin/v1/tenants/{db.a}/audit-events")
        assert len(mine) == 2
        assert {item["tenant_id"] for item in mine} == {str(db.a)}
        assert len({item["id"] for item in mine}) == 2
        assert (await tenant_auditor.get("/admin/v1/audit-events")).status_code == 403
        assert (
            await tenant_auditor.get(f"/admin/v1/tenants/{db.b}/audit-events")
        ).status_code == 404
    async with _client(app, "trustbridge:auditor", sub="carol") as instance:
        everything = await _collect(instance, "/admin/v1/audit-events")
        tenants = {item["tenant_id"] for item in everything}
        assert {str(db.a), str(db.b), None} <= tenants
        narrowed = (
            await instance.get("/admin/v1/audit-events", params={"tenant_id": str(db.b)})
        ).json()
        assert {item["tenant_id"] for item in narrowed["items"]} == {str(db.b)}
        desc = (
            await instance.get("/admin/v1/audit-events", params={"descending": "true", "limit": 3})
        ).json()
        seqs = [item["seq"] for item in desc["items"]]
        assert seqs == sorted(seqs, reverse=True)
        assert desc["next_cursor"]


async def test_the_export_is_ordered_paged_scoped_and_audited(db: rls.Db) -> None:
    """Inv. 11/13 (ADR 0032): NDJSON in seq order across pages, scoped, one event per export."""
    await _seed(db)
    app = _app(db)
    before = (await _rows(db, "SELECT count(*) FROM audit_events WHERE action = 'audit.export'"))[
        0
    ][0]
    async with _client(app, f"trustbridge:{HOST}:a:auditor", sub="bob") as tenant_auditor:
        response = await tenant_auditor.get(f"/admin/v1/tenants/{db.a}/audit-events/export")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")
        import json  # noqa: PLC0415

        lines = [json.loads(line) for line in response.text.splitlines()]
        assert {line["tenant_id"] for line in lines} == {str(db.a)}
        assert [line["seq"] for line in lines] == sorted(line["seq"] for line in lines)
        assert len(lines) >= 2
        assert (await tenant_auditor.get("/admin/v1/audit-events/export")).status_code == 403
    async with _client(app, "trustbridge:auditor", sub="carol") as instance:
        full = await instance.get("/admin/v1/audit-events/export", params={"action": "tenant.sync"})
        everything = [json.loads(line) for line in full.text.splitlines()]
        assert len({e["seq"] for e in everything}) == len(everything) >= 5
        resumed = await instance.get(
            "/admin/v1/audit-events/export",
            params={"action": "tenant.sync", "since_seq": everything[1]["seq"]},
        )
        assert [json.loads(line)["seq"] for line in resumed.text.splitlines()] == [
            e["seq"] for e in everything[2:]
        ]
    events = await _rows(
        db,
        'SELECT actor_sub, tenant_id, resource_id, outcome, "after" FROM audit_events '
        "WHERE action = 'audit.export' ORDER BY seq",
    )
    assert len(events) - before == 3
    bob, carol, resumed_event = events[-3:]
    assert (bob.actor_sub, bob.tenant_id, bob.outcome) == ("bob", db.a, "success")
    assert bob.after["row_count"] == len(lines)
    assert bob.after["complete"] is True
    assert (carol.actor_sub, carol.tenant_id, carol.resource_id) == ("carol", None, "instance")
    assert carol.after["action"] == "tenant.sync"
    assert resumed_event.after["after_seq"] == everything[1]["seq"]
    assert carol.after["row_count"] == len(everything)


async def test_no_audit_route_changes_a_row(db: rls.Db) -> None:
    """Inv. 14 (ADR 0032): only GET exists on the audit routes."""
    app = _app(db)
    async with _client(app, "trustbridge:auditor") as client:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            response = await client.request(method, "/admin/v1/audit-events")
            assert response.status_code in {404, 405}


@pytest.fixture(autouse=True)
async def _no_leftover_context() -> AsyncIterator[None]:
    yield
    assert current_tenant.get() is None


async def test_all_tenants_lists_every_tenant_ordered_by_id(db: rls.Db) -> None:
    """The real read behind `/tenants`: both tenants, sorted, whatever the caller's context."""
    rows = await all_tenants(db.app)
    assert [t.id for t in rows] == sorted([db.a, db.b], key=str)
    assert {t.org for t in rows} == {"a", "b"}
