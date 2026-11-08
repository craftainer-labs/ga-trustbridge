"""The admin read routes over fakes: guards, errors, pagination, filters (ADR 0033 inv. 1-13)."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from tests.trustbridge.admin import helpers
from tests.trustbridge.admin.helpers import ACME, OTHER, build, problem
from trustbridge.admin import guards, routes
from trustbridge.admin.access import NoTenants, tenant_ref
from trustbridge.admin.cursor import encode_cursor
from trustbridge.admin.guards import GUARDED, LIMITS
from trustbridge.audit import ActorType, AuditPage, AuditQuery, AuditRecord, Outcome
from trustbridge.rbac import Principal, RbacConfig, RbacRuntime, get_principal, tenant_roles
from trustbridge.tenancy import TenantInfo, current_tenant

ADMIN = "trustbridge:admin"
VIEWER = "trustbridge:viewer"
AUDITOR = "trustbridge:auditor"
ACME_AUDITOR = "trustbridge:github.com:acme:auditor"
ACME_VIEWER = "trustbridge:github.com:acme:viewer"


def _info(ref: Any, state: str = "active", disabled: bool = False) -> TenantInfo:  # noqa: ANN401
    return TenantInfo(
        ref.id,
        ref.host,
        7,
        ref.org,
        70,
        state,
        datetime(2026, 1, 1, tzinfo=UTC) if disabled else None,
        False,
    )


def _record(seq: int, tenant: UUID | None = None) -> AuditRecord:
    return AuditRecord(
        id=uuid4(),
        seq=seq,
        occurred_at=helpers.when(),
        recorded_at=helpers.when(),
        tenant_id=tenant,
        actor_type=ActorType.USER,
        actor_sub="u1",
        actor_iss="https://idp.example",
        on_behalf_of=None,
        source_ip=None,
        action="authz.denied",
        resource_type="role",
        resource_id="admin",
        outcome=Outcome.DENIED,
        before=None,
        after={"reason": "role_missing"},
        request_id=None,
        trace_id=None,
        schema_version=1,
    )


@pytest.fixture
def tenants(monkeypatch: pytest.MonkeyPatch) -> list[TenantInfo]:
    """Two tenants as `tb_list_tenants()` returns them (ACME disabled)."""
    rows = [_info(ACME, disabled=True), _info(OTHER)]

    async def tenants_page(
        _factory: object, principal: Principal, *, after: UUID | None, limit: int
    ) -> list[TenantInfo]:
        mine = [t for t in rows if tenant_roles(principal, tenant_ref(t))]
        ordered = sorted(mine, key=lambda t: str(t.id))
        return [t for t in ordered if after is None or str(t.id) > str(after)][:limit]

    async def get_tenant(_factory: object, tenant_id: UUID) -> TenantInfo | None:
        return next((t for t in rows if t.id == tenant_id), None)

    monkeypatch.setattr(routes, "tenants_page", tenants_page)
    monkeypatch.setattr(routes, "get_tenant", get_tenant)
    return rows


# ---- the router walk (inv. 1, 13, 21) ------------------------------------------------------


def test_every_admin_route_authenticates_limits_then_checks_the_role_under_v1() -> None:
    """Inv. 1/13/21: authenticate, then the limit, then a recorded role guard."""
    api_routes = [r for r in routes.router.routes if isinstance(r, APIRoute)]
    assert api_routes
    for route in api_routes:
        assert route.path.startswith("/admin/v1/"), route.path
        calls = [d.call for d in route.dependant.dependencies]
        assert len(calls) >= 3, route.path
        assert calls[0] is get_principal, route.path
        assert calls[1] in LIMITS, route.path
        assert calls[2] in GUARDED, route.path
    assert guards.mutations_without_emitter(routes.router.routes) == []  # inv. 17 (ADR 0032 inv. 8)


def test_the_spec_lists_only_admin_routes_and_needs_a_token() -> None:
    """Inv. 11 (first half): generated from the admin router only, authenticated."""
    env = build()
    anonymous = TestClient(env.app)
    problem(anonymous.get("/admin/v1/openapi.json"), 401, "unauthenticated")
    spec = env.client().get("/admin/v1/openapi.json").json()
    assert spec["openapi"].startswith("3.")
    assert spec["paths"]
    assert all(path.startswith("/admin/v1/") for path in spec["paths"])
    assert "/other" not in spec["paths"]


# ---- authentication, authorization, errors ---------------------------------------------------


def test_no_token_and_a_bad_token_are_401_problems_with_the_challenge() -> None:
    """Inv. 2: 401 problem+json with the RFC 9728 challenge; nothing is audited."""
    env = build()
    anonymous = TestClient(env.app)
    response = anonymous.get("/admin/v1/hosts")
    problem(response, 401, "unauthenticated")
    assert "resource_metadata" in response.headers["www-authenticate"]
    bad = anonymous.get("/admin/v1/hosts", headers={"Authorization": "Bearer nope"})
    problem(bad, 401, "unauthenticated")
    assert env.sink.denials == []


def test_a_caller_without_a_role_is_403_and_the_denial_carries_the_sub() -> None:
    """Inv. 3."""
    env = build()
    body = problem(env.client(sub="mallory").get("/admin/v1/hosts"), 403, "forbidden")
    assert body["detail"] == "Insufficient role"
    assert [(d.sub, d.reason) for d in env.sink.denials] == [("mallory", "role_missing")]


def test_errors_off_the_admin_path_keep_the_templates_type() -> None:
    """Only /admin/v1 gets the tag: URIs."""
    env = build()
    body = problem(env.client().get("/other"), 418)
    assert body["type"] == "about:blank"


def test_unknown_route_under_v1_is_a_typed_404() -> None:
    """The routing 404 is a problem document too."""
    problem(build().client(ADMIN).get("/admin/v1/nope"), 404, "not-found")


@pytest.mark.parametrize("limit", ["0", "-1", "201", "x"])
def test_a_bad_limit_is_a_422_problem(limit: str) -> None:
    """Inv. 10: a larger or non-positive limit is rejected."""
    problem(
        build().client(VIEWER).get("/admin/v1/hosts", params={"limit": limit}), 422, "validation"
    )


def test_a_malformed_cursor_is_a_400_problem() -> None:
    """Decision A3."""
    for bad in ("!!!", encode_cursor(1, "x")[:-3], "e30"):
        problem(
            build().client(VIEWER).get("/admin/v1/hosts", params={"cursor": bad}),
            400,
            "invalid-cursor",
        )


# ---- /me, hosts, tenants -----------------------------------------------------------------


def test_me_lists_the_grants_and_reachable_tenants(tenants: list[TenantInfo]) -> None:
    """The principal view; a tenant grant reaches only its tenant."""
    del tenants
    env = build()
    body = env.client(ACME_VIEWER, "security").get("/admin/v1/me").json()
    assert body["sub"] == "u1"
    assert body["iss"] == "https://idp.example"
    assert {(g["role"], g["scope"]) for g in body["grants"]} == {
        ("viewer", "tenant"),
        ("auditor", "instance"),
    }
    assert {t["org"] for t in body["tenants"]} == {"acme", "other"}  # security is instance-wide


def test_me_for_a_principal_with_no_roles_is_not_an_error(tenants: list[TenantInfo]) -> None:
    """Inv. 4 tail: no roles, empty grants."""
    del tenants
    body = build().client().get("/admin/v1/me").json()
    assert body["grants"] == []
    assert body["tenants"] == []


def test_hosts_page_without_repeats_and_unknown_host_is_404() -> None:
    """Instance viewer; the cursor resumes after the last host sent."""
    client = build().client(VIEWER)
    first = client.get("/admin/v1/hosts", params={"limit": 1}).json()
    assert [h["name"] for h in first["items"]] == ["ghe.example"]
    second = client.get(
        "/admin/v1/hosts", params={"limit": 1, "cursor": first["next_cursor"]}
    ).json()
    assert [h["name"] for h in second["items"]] == ["github.com"]
    assert second["next_cursor"] is None
    assert client.get("/admin/v1/hosts/github.com").json() == {
        "name": "github.com",
        "state": "ready",
        "reason": None,
    }
    problem(client.get("/admin/v1/hosts/nope"), 404, "not-found")


def test_a_tenant_scoped_grant_cannot_read_instance_data() -> None:
    """Inv. 7: hosts are instance-scoped."""
    problem(build().client(ACME_VIEWER).get("/admin/v1/hosts"), 403, "forbidden")


def test_tenants_are_filtered_by_grants_and_show_both_states(tenants: list[TenantInfo]) -> None:
    """Per-grants list: a tenant grant sees its tenant only; effective state combines both."""
    del tenants
    env = build()
    mine = env.client(ACME_VIEWER).get("/admin/v1/tenants").json()
    assert [t["org"] for t in mine["items"]] == ["acme"]
    item = mine["items"][0]
    assert (item["state"], item["effective_state"], item["deleting"]) == (
        "active",
        "disabled",
        False,
    )
    assert item["admin_disabled_at"] is not None
    everything = env.client(VIEWER).get("/admin/v1/tenants").json()
    assert {t["org"] for t in everything["items"]} == {"acme", "other"}
    assert {t["org"]: t["effective_state"] for t in everything["items"]}["other"] == "active"
    problem(env.client().get("/admin/v1/tenants"), 403, "forbidden")


def test_tenants_page_and_cursor(tenants: list[TenantInfo]) -> None:
    """Inv. 10: the cursor resumes without repeating or skipping, also when a row appears."""
    env = build()
    client = env.client(VIEWER)
    first = client.get("/admin/v1/tenants", params={"limit": 1}).json()
    assert len(first["items"]) == 1
    newcomer = replace(tenants[0], id=UUID(int=0), org="aaa")  # sorts before everything
    tenants.append(newcomer)
    second = client.get(
        "/admin/v1/tenants", params={"limit": 5, "cursor": first["next_cursor"]}
    ).json()
    seen = [first["items"][0]["id"], *(t["id"] for t in second["items"])]
    assert len(seen) == len(set(seen)) == 2  # the newcomer sorted before the cursor: not shown


def test_tenant_read_unknown_and_ungranted_look_the_same(tenants: list[TenantInfo]) -> None:
    """Inv. 5: 404 with one body, and an authz.denied with the requested id."""
    del tenants
    env = build()
    client = env.client(ACME_VIEWER)
    assert client.get(f"/admin/v1/tenants/{ACME.id}").json()["org"] == "acme"
    other = problem(client.get(f"/admin/v1/tenants/{OTHER.id}"), 404, "not-found")
    unknown_id = uuid4()
    unknown = problem(client.get(f"/admin/v1/tenants/{unknown_id}"), 404, "not-found")
    assert other["detail"] == unknown["detail"]
    assert [d.tenant_id for d in env.sink.denials] == [str(OTHER.id), str(unknown_id)]
    assert {d.reason for d in env.sink.denials} == {"tenant_not_granted"}


# ---- audit list ----------------------------------------------------------------------------


class Reads:
    """Scripted `list_events`: records the calls and serves the pages."""

    def __init__(self, pages: list[AuditPage]) -> None:
        """Serve `pages` in order."""
        self.pages = pages
        self.calls: list[tuple[AuditQuery, UUID | None, bool]] = []

    async def __call__(
        self,
        _session: object,
        query: AuditQuery,
        *,
        tenant_id: UUID | None = None,
        instance_scope: bool = False,
    ) -> AuditPage:
        """Record and answer."""
        self.calls.append((query, tenant_id, instance_scope))
        return (
            self.pages[len(self.calls) - 1]
            if len(self.calls) <= len(self.pages)
            else self.pages[-1]
        )


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> Reads:
    """Patch `list_events` in the routes module."""
    fake = Reads([AuditPage([_record(5), _record(6)], 6)])
    monkeypatch.setattr(routes, "list_events", fake)
    return fake


def test_only_an_auditor_reads_audit(reads: Reads) -> None:
    """Inv. 11 (ADR 0032): admin and operator get 403 on every audit route."""
    env = build()
    for role in (ADMIN, "trustbridge:operator", VIEWER):
        client = env.client(role)
        problem(client.get("/admin/v1/audit-events"), 403, "forbidden")
        problem(client.get("/admin/v1/audit-events/export"), 403, "forbidden")
        problem(client.get(f"/admin/v1/tenants/{ACME.id}/audit-events"), 403, "forbidden")
        problem(client.get(f"/admin/v1/tenants/{ACME.id}/audit-events/export"), 403, "forbidden")
    assert reads.calls == []


def test_a_tenant_auditor_reads_its_tenant_only_and_not_the_instance_routes(reads: Reads) -> None:
    """Inv. 7/11: tenant scope on the tenant route; 403 on the instance route; 404 on another."""
    client = build().client(ACME_AUDITOR)
    ok = client.get(f"/admin/v1/tenants/{ACME.id}/audit-events")
    assert ok.status_code == 200
    assert reads.calls[0][1:] == (ACME.id, False)
    problem(client.get("/admin/v1/audit-events"), 403, "forbidden")
    problem(client.get(f"/admin/v1/tenants/{OTHER.id}/audit-events"), 404, "not-found")
    assert len(reads.calls) == 1


def test_an_instance_auditor_lists_with_filters_and_a_cursor(reads: Reads) -> None:
    """The envelope, the AuditQuery filter names, and the cursor in both directions."""
    client = build().client(AUDITOR)
    some = uuid4()
    response = client.get(
        "/admin/v1/audit-events",
        params={
            "limit": 2,
            "tenant_id": str(some),
            "action": "authz.denied",
            "actor_sub": "u1",
            "resource_type": "role",
            "resource_id": "admin",
            "outcome": "denied",
            "since": "2026-11-01T00:00:00Z",
            "until": "2026-11-05T00:00:00+01:00",
        },
    )
    body = response.json()
    assert [i["seq"] for i in body["items"]] == [5, 6]
    assert body["items"][0]["outcome"] == "denied"
    assert body["next_cursor"]
    query, tenant_id, instance = reads.calls[0]
    assert (tenant_id, instance) == (None, True)
    assert (query.tenant_id, query.action, query.actor_sub, query.outcome) == (
        some,
        "authz.denied",
        "u1",
        Outcome.DENIED,
    )
    assert (query.resource_type, query.resource_id, query.limit) == ("role", "admin", 2)
    assert query.since == datetime(2026, 11, 1, tzinfo=UTC)
    client.get("/admin/v1/audit-events", params={"cursor": body["next_cursor"]})
    assert (reads.calls[1][0].after_seq, reads.calls[1][0].before_seq) == (6, None)
    client.get(
        "/admin/v1/audit-events", params={"cursor": body["next_cursor"], "descending": "true"}
    )
    assert (reads.calls[2][0].after_seq, reads.calls[2][0].before_seq) == (None, 6)
    assert reads.calls[2][0].descending


def test_audit_filters_are_validated(reads: Reads) -> None:
    """A naive time, a bad outcome, an over-long value or a string cursor key are 4xx problems."""
    client = build().client(AUDITOR)
    problem(
        client.get("/admin/v1/audit-events", params={"since": "2026-11-01T00:00:00"}),
        422,
        "validation",
    )
    problem(client.get("/admin/v1/audit-events", params={"outcome": "maybe"}), 422, "validation")
    problem(client.get("/admin/v1/audit-events", params={"action": "x" * 65}), 422, "validation")
    problem(
        client.get("/admin/v1/audit-events", params={"cursor": encode_cursor("s", "i")}),
        400,
        "invalid-cursor",
    )
    assert reads.calls == []


def test_no_database_is_a_503_problem() -> None:
    """Mock mode without a session factory."""
    env = build(factory=None)
    problem(env.client(AUDITOR).get("/admin/v1/audit-events"), 503, "unavailable")
    problem(env.client(AUDITOR).get("/admin/v1/audit-events/export"), 503, "unavailable")


def test_no_audit_route_writes_or_deletes() -> None:
    """ADR 0032 inv. 14: no audit route writes; the only POSTs are the NG6e mutations."""
    posts = {
        r.path
        for r in routes.router.routes
        if isinstance(r, APIRoute) and "POST" in (r.methods or set())
    }
    assert posts == {
        "/admin/v1/tenants/{tenant_id}/disable",
        "/admin/v1/tenants/{tenant_id}/enable",
        "/admin/v1/tenants/{tenant_id}/reconcile",
        "/admin/v1/deliveries/{host}/{delivery_id}/requeue",
    }
    others = {
        m
        for r in routes.router.routes
        if isinstance(r, APIRoute)
        for m in (r.methods or set())
        if m != "POST"
    }
    assert others == {"GET"}
    env = build()
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert env.client(AUDITOR).request(method, "/admin/v1/audit-events").status_code in {
            404,
            405,
        }


# ---- audit export -------------------------------------------------------------------------


class Exports:
    """Scripted `record_export`."""

    def __init__(self) -> None:
        """Start empty."""
        self.events: list[dict[str, Any]] = []

    async def __call__(self, writer: object, **kwargs: Any) -> None:  # noqa: ANN401
        """Record the event's facts."""
        self.events.append({"writer": writer, **kwargs})


@pytest.fixture
def exported(monkeypatch: pytest.MonkeyPatch) -> Exports:
    """Patch `record_export` in the routes module."""
    fake = Exports()
    monkeypatch.setattr(routes, "record_export", fake)
    return fake


def _paged(monkeypatch: pytest.MonkeyPatch) -> Reads:
    fake = Reads(
        [
            AuditPage([_record(1), _record(2)], 2),
            AuditPage([_record(3)], None),
        ]
    )
    monkeypatch.setattr(routes, "list_events", fake)
    return fake


def test_instance_export_streams_ndjson_in_pages_and_writes_its_event(
    monkeypatch: pytest.MonkeyPatch, exported: Exports
) -> None:
    """Inv. 13 (ADR 0032): NDJSON, lag-bounded, resumable, and the export's own event."""
    reads = _paged(monkeypatch)
    env = build(writer=object())
    response = env.client(AUDITOR).get(
        "/admin/v1/audit-events/export", params={"action": "authz.denied", "since_seq": 0}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="audit-events-instance.ndjson"'
    )
    lines = [json.loads(line) for line in response.text.splitlines()]
    assert [line["seq"] for line in lines] == [1, 2, 3]
    assert list(lines[0])[:4] == ["id", "seq", "occurred_at", "recorded_at"]
    first, second = (call[0] for call in reads.calls)
    assert (first.lag_seconds, first.descending, first.limit, first.after_seq) == (0.5, False, 2, 0)
    assert second.after_seq == 2
    assert all(call[2] and call[1] is None for call in reads.calls)
    (event,) = exported.events
    assert (event["sub"], event["iss"], event["tenant_id"]) == ("u1", "https://idp.example", None)
    assert (event["row_count"], event["last_seq"], event["complete"]) == (3, 3, True)
    assert event["query"].action == "authz.denied"


def test_a_failed_audit_write_is_logged_and_does_not_break_the_stream(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A DB outage while recording the export is logged, not raised over the stream."""

    async def broken(writer: object, **kwargs: Any) -> None:  # noqa: ANN401
        del writer, kwargs
        msg = "db down"
        raise RuntimeError(msg)

    _paged(monkeypatch)
    monkeypatch.setattr(routes, "record_export", broken)
    env = build(writer=object())
    with caplog.at_level("ERROR", logger=routes.logger.name):
        response = env.client(AUDITOR).get("/admin/v1/audit-events/export")
    assert response.status_code == 200
    assert [json.loads(line)["seq"] for line in response.text.splitlines()] == [1, 2, 3]
    assert "audit.export row not written; 3 rows were sent" in caplog.text


def test_tenant_export_runs_in_the_tenants_scope(
    monkeypatch: pytest.MonkeyPatch, exported: Exports
) -> None:
    """The tenant route reads the tenant's rows and the event names the tenant."""
    reads = _paged(monkeypatch)
    env = build(writer=object())
    response = env.client(ACME_AUDITOR).get(f"/admin/v1/tenants/{ACME.id}/audit-events/export")
    assert response.status_code == 200
    assert all(call[1:] == (ACME.id, False) for call in reads.calls)
    assert exported.events[0]["tenant_id"] == ACME.id
    assert f"audit-events-{ACME.id}" in response.headers["content-disposition"]


def test_a_failed_export_still_writes_an_incomplete_event(
    monkeypatch: pytest.MonkeyPatch, exported: Exports
) -> None:
    """The event is written in `finally`: a stream that dies mid-way says so and where to resume."""
    calls = 0

    async def broken(*_a: object, **_k: object) -> AuditPage:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("database went away")
        return AuditPage([_record(1)], 1)

    monkeypatch.setattr(routes, "list_events", broken)
    env = build(writer=object())
    client = env.client(AUDITOR)
    with pytest.raises(RuntimeError, match="database went away"):
        client.get("/admin/v1/audit-events/export")
    (event,) = exported.events
    assert (event["row_count"], event["last_seq"], event["complete"]) == (1, 1, False)


def test_exports_have_their_own_small_bucket(
    monkeypatch: pytest.MonkeyPatch, exported: Exports
) -> None:
    """Inv. 14: exports and reads count separately."""
    _paged(monkeypatch)
    env = build(writer=object(), admin_rate_limit_export_requests=1)
    client = env.client(AUDITOR)
    assert client.get("/admin/v1/audit-events/export").status_code == 200
    limited = client.get("/admin/v1/audit-events/export")
    problem(limited, 429, "rate-limited")
    assert int(limited.headers["retry-after"]) >= 1
    assert len(exported.events) == 1
    assert client.get("/admin/v1/hosts").status_code == 200  # a read: its own bucket


def test_a_tenant_that_vanished_between_the_check_and_the_read_is_a_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RBAC saw the tenant, the listing no longer does."""

    async def none(_factory: object, _tenant_id: UUID) -> None:
        return None

    monkeypatch.setattr(routes, "get_tenant", none)
    problem(build().client(ACME_VIEWER).get(f"/admin/v1/tenants/{ACME.id}"), 404, "not-found")


def test_a_rejection_without_a_runtime_is_still_typed() -> None:
    """The per-address bucket needs `app.state.admin`; without it only the type is rewritten."""
    env = build()
    env.app.state.admin = None
    problem(env.client(VIEWER).get("/admin/v1/nope"), 404, "not-found")


def test_a_missing_runtime_is_a_server_error_not_an_open_door() -> None:
    """A guarded route on an app that never set `app.state.admin` fails (500), never passes."""
    env = build()
    env.app.state.admin = None
    client = TestClient(
        env.app,
        client=("testclient", 1),
        headers=helpers.token("u1", VIEWER),
        raise_server_exceptions=False,
    )
    assert client.get("/admin/v1/hosts").status_code == 500


def test_a_principal_with_no_grant_cannot_list_tenants_even_without_a_denial_sink() -> None:
    """403 for no grant at all; the sink is optional."""
    env = build()
    env.app.state.rbac = RbacRuntime(RbacConfig(helpers.CLIENT), helpers.Access([]))
    problem(env.client().get("/admin/v1/tenants"), 403, "forbidden")


def test_me_without_a_database_lists_no_tenants() -> None:
    """Mock mode: the grants are shown, no tenant is resolved."""
    body = build(factory=None).client(VIEWER).get("/admin/v1/me").json()
    assert body["tenants"] == []
    assert [g["role"] for g in body["grants"]] == ["viewer"]


async def test_no_tenants_access_resolves_nothing() -> None:
    """`MODE=mock` without a database."""
    access = NoTenants()
    assert await access.get_tenant(uuid4()) is None
    tenant = uuid4()
    with access.tenant_context(tenant):
        assert current_tenant.get() == tenant
    assert current_tenant.get() is None


def test_a_tenants_cursor_must_name_a_tenant_id(tenants: list[TenantInfo]) -> None:
    """The tenant list resumes after an id; any other key is a 400."""
    del tenants
    client = build().client(VIEWER)
    for bad in (encode_cursor("not-a-uuid", "x"), encode_cursor(5, "5")):
        problem(client.get("/admin/v1/tenants", params={"cursor": bad}), 400, "invalid-cursor")
    ok = client.get("/admin/v1/tenants", params={"cursor": encode_cursor(str(UUID(int=0)), "x")})
    assert ok.status_code == 200


def test_me_caps_the_tenants_at_200_and_says_when_more_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User decision (NG6d-2): the first 200, and `tenants_truncated` when there are more."""
    rows = [_info(ACME)] * 201

    async def many(
        _factory: object, _principal: Principal, *, after: UUID | None, limit: int
    ) -> list[TenantInfo]:
        assert after is None
        return rows[:limit]

    monkeypatch.setattr(routes, "tenants_page", many)
    client = build().client(VIEWER)
    body = client.get("/admin/v1/me").json()
    assert len(body["tenants"]) == 200
    assert body["tenants_truncated"] is True
    rows[:] = rows[:200]
    exact = client.get("/admin/v1/me").json()
    assert len(exact["tenants"]) == 200
    assert exact["tenants_truncated"] is False


def test_me_is_not_truncated_for_a_small_list(tenants: list[TenantInfo]) -> None:
    """Few tenants, no flag."""
    del tenants
    body = build().client(VIEWER).get("/admin/v1/me").json()
    assert body["tenants_truncated"] is False
