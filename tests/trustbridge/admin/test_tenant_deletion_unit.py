"""The tenant-deletion admin routes over a scripted database (ADR 0034, NG6h-7b).

The definer functions run in the integration suite (`tenancy/test_deletion_skip_integration.py`);
here the routes' own work is checked: the guards, the bodies, the answers, the problem types, the
audit events (one per success, a `failure` event with a fixed reason per refusal) and the tenant
context the events are written in.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

from tests.trustbridge.admin.helpers import ACME, FakeSession, build, problem
from trustbridge.admin import deletions, guards, routes, views
from trustbridge.admin.cursor import decode_cursor
from trustbridge.audit import Action, AuditEvent, Outcome
from trustbridge.rbac import Role

ADMIN = "trustbridge:admin"
OPERATOR = "trustbridge:operator"
VIEWER = "trustbridge:viewer"
ACME_ADMIN = "trustbridge:github.com:acme:admin"
DELETION = UUID(int=0xD1)
TENANT = ACME.id
AT = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
CANARY = "canary-secret-7f3a9c1e"
REQUEST = f"/admin/v1/tenants/{TENANT}/deletion"
BASE = f"/admin/v1/tenant-deletions/{DELETION}"
BODY = {
    "host": "github.com",
    "installation_id": 7,
    "organization": "acme",
    "reason": "customer request",
}


def _record(**changes: Any) -> dict[str, Any]:  # noqa: ANN401 -- a result row
    values: dict[str, Any] = {
        "id": DELETION,
        "tenant_id": TENANT,
        "host": "github.com",
        "installation_id": 7,
        "state": "requested",
        "delete_platform_resources": False,
        "not_before": None,
        "requested_at": AT,
        "requested_by_sub": "u1",
        "counts": {},
        "skipped": [],
        "attempts": 0,
        "last_error": None,
        "updated_at": AT,
    }
    values.update(changes)
    return values


class Result:
    """What a statement returns: one row, a scalar, or mapping rows."""

    def __init__(self, rows: list[dict[str, Any]] | None = None, one: Any = None) -> None:  # noqa: ANN401
        """Hold the rows or the single row."""
        self.rows, self._one = rows or [], one

    def mappings(self) -> Result:
        """Rows as mappings."""
        return self

    def __iter__(self) -> Iterator[dict[str, Any]]:
        """Iterate the rows."""
        return iter(self.rows)

    def first(self) -> dict[str, Any] | None:
        """The first row, if any."""
        return self.rows[0] if self.rows else None

    def one(self) -> Any:  # noqa: ANN401
        """The single row."""
        return self._one

    def scalar_one(self) -> Any:  # noqa: ANN401
        """The single value."""
        return self._one


class World:
    """A scripted database: what each definer answers, and what the routes ran and appended."""

    def __init__(self) -> None:
        """A `requested` deletion, and every function succeeding."""
        self.record: dict[str, Any] | None = _record()
        self.records: list[dict[str, Any]] | None = None
        self.request = SimpleNamespace(outcome="created", deletion_id=DELETION)
        self.cancel = "cancelled"
        self.skip = SimpleNamespace(outcome="skipped", from_state="platform", to_state="github")
        self.error: Exception | None = None
        self.statements: list[tuple[str, dict[str, Any]]] = []
        self.events: list[AuditEvent] = []
        self.written: list[AuditEvent] = []
        self.contexts: list[UUID | None] = []

    def factory(self) -> Any:  # noqa: ANN401
        """A session factory over this world."""
        world = self

        class Session(FakeSession):
            async def execute(self, statement: object, params: dict[str, Any] | None = None) -> Any:  # noqa: ANN401
                sql = str(statement)
                world.statements.append((sql, params or {}))
                if "tb_request_tenant_deletion" in sql:
                    return Result(one=world.request)
                if "tb_cancel_tenant_deletion" in sql:
                    return Result(one=world.cancel)
                if "tb_skip_tenant_deletion_step" in sql:
                    return Result(one=world.skip)
                if "tb_operator_fail_tenant_deletion" in sql:
                    if world.error is not None:
                        raise world.error
                    return Result()
                if "WHERE id = :id" in sql:
                    return Result([] if world.record is None else [world.record])
                return Result(world.records if world.records is not None else [_record()])

        return Session

    def ran(self, name: str) -> list[dict[str, Any]]:
        """The parameters of each statement that called `name`."""
        return [p for sql, p in self.statements if name in sql]


class Writer:
    """An `AuditWriter` stand-in recording into the world."""

    def __init__(self, world: World, error: Exception | None = None) -> None:
        """Record into `world.written`, or raise `error`."""
        self.world, self.error = world, error

    async def record(self, event: AuditEvent) -> UUID:
        """Record the event or raise."""
        if self.error is not None:
            raise self.error
        self.world.written.append(event)
        return event.id


def _client(
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    *roles: str,
    grace: int = 0,
    server_errors: bool = True,
    writer: bool | Exception = True,
) -> TestClient:
    env = build(factory=world.factory())
    runtime = replace(env.runtime, deletion_grace_seconds=grace)
    if writer is not False:
        runtime = replace(
            runtime,
            writer=Writer(world, writer if isinstance(writer, Exception) else None),  # type: ignore[arg-type]
        )
    env.app.state.admin = runtime

    async def append(session: object, event: AuditEvent) -> UUID:
        del session
        world.events.append(event)
        return event.id

    @contextmanager
    def context(tenant_id: UUID | None) -> Iterator[None]:
        world.contexts.append(tenant_id)
        yield

    monkeypatch.setattr(routes, "append", append)
    monkeypatch.setattr(routes, "tenant_context", context)
    client = env.client(*roles)
    if server_errors:
        return client
    return TestClient(
        env.app,
        client=("testclient", 50000),
        raise_server_exceptions=False,
        headers=dict(client.headers),
    )


class _Driver(Exception):  # noqa: N818 -- stands in for the driver's error
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def _db_error(sqlstate: str) -> DBAPIError:
    return DBAPIError("SELECT 1", {}, _Driver(sqlstate))


# ---- the guards --------------------------------------------------------------------------


def test_every_deletion_route_has_its_role_bucket_and_emitter() -> None:
    """Writes: instance `admin` on the mutation bucket and one `emits`; reads: instance `viewer`."""
    api = [r for r in routes.router.routes if isinstance(r, APIRoute)]
    expected = {
        routes.request_tenant_deletion: (Role.ADMIN, "tenant_id", True, "mutation"),
        routes.cancel_tenant_deletion: (Role.ADMIN, None, False, "mutation"),
        routes.skip_tenant_deletion_step: (Role.ADMIN, None, False, "mutation"),
        routes.fail_tenant_deletion: (Role.ADMIN, None, False, "mutation"),
        routes.list_tenant_deletions: (Role.VIEWER, None, False, "read"),
        routes.get_tenant_deletion: (Role.VIEWER, None, False, "read"),
    }
    for endpoint, (role, param, instance_only, bucket) in expected.items():
        (route,) = [r for r in api if r.endpoint is endpoint]
        calls = [d.call for d in route.dependant.dependencies]
        assert [
            guards.GUARDED[c]
            for c in calls
            if c in guards.GUARDED and guards.GUARDED[c][0] is not None
        ] == [(role, param, instance_only)]
        assert [guards.LIMIT_BUCKETS[c] for c in calls if c in guards.LIMIT_BUCKETS] == [bucket]
    assert guards.EMITTERS[routes.request_tenant_deletion] is Action.TENANT_DELETION_REQUESTED
    assert guards.EMITTERS[routes.cancel_tenant_deletion] is Action.TENANT_DELETION_CANCELLED
    assert guards.EMITTERS[routes.skip_tenant_deletion_step] is Action.TENANT_DELETION_STEP_SKIPPED
    assert guards.EMITTERS[routes.fail_tenant_deletion] is Action.TENANT_DELETION_OPERATOR_FAILED
    assert guards.mutations_without_emitter(routes.router.routes) == []


def test_the_action_names_are_the_decided_ones() -> None:
    """The user's two names (G-api, 2026-11-16) and the two that follow them."""
    assert Action.TENANT_DELETION_STEP_SKIPPED.value == "tenant.deletion_step_skipped"
    assert Action.TENANT_DELETION_OPERATOR_FAILED.value == "tenant.deletion_failed"
    assert Action.TENANT_DELETION_REQUESTED.value == "tenant.deletion_requested"
    assert Action.TENANT_DELETION_CANCELLED.value == "tenant.deletion_cancelled"


@pytest.mark.parametrize("role", [OPERATOR, VIEWER, ACME_ADMIN])
def test_only_an_instance_admin_may_change_a_deletion(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """Operator, viewer and a tenant-scoped admin are refused on all four writes."""
    world = World()
    client = _client(monkeypatch, world, role)
    problem(client.post(REQUEST, json=BODY), 403, "forbidden")
    problem(client.post(f"{BASE}/cancel"), 403, "forbidden")
    problem(client.post(f"{BASE}/skip-step", json={"reason": "x"}), 403, "forbidden")
    problem(client.post(f"{BASE}/fail", json={"reason": "x"}), 403, "forbidden")
    assert world.statements == []


def test_reads_need_instance_viewer_and_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tenant-scoped role does not read the host-wide list; no token is a 401."""
    world = World()
    client = _client(monkeypatch, world, ACME_ADMIN)
    problem(client.get("/admin/v1/tenant-deletions"), 403, "forbidden")
    problem(client.get(BASE), 403, "forbidden")
    anonymous = TestClient(build(factory=world.factory()).app)
    problem(anonymous.get(BASE), 401, "unauthenticated")


# ---- request -----------------------------------------------------------------------------


def test_a_request_writes_the_record_and_one_event_and_answers_202(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The typed confirmation goes to the function, the event has ids and the reason only."""
    world = World()
    response = _client(monkeypatch, world, ADMIN).post(
        REQUEST, json={**BODY, "delete_platform_resources": True}
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["id"] == str(DELETION)
    assert body["state"] == "requested"
    assert body["hint"] == views.UNINSTALL_HINT
    (params,) = world.ran("tb_request_tenant_deletion")
    assert params == {
        "tenant": TENANT,
        "host": "github.com",
        "installation": 7,
        "org": "acme",
        "sub": "u1",
        "iss": "https://idp.example",
        "platform": True,
        "not_before": None,
    }
    (event,) = world.events
    assert (event.action, event.outcome, event.tenant_id) == (
        Action.TENANT_DELETION_REQUESTED,
        Outcome.SUCCESS,
        TENANT,
    )
    assert (event.resource_type, event.resource_id) == ("tenant_deletion", str(DELETION))
    assert (event.actor_sub, event.actor_iss) == ("u1", "https://idp.example")
    assert event.before is None
    assert event.after == {
        "delete_platform_resources": False,
        "deletion_id": str(DELETION),
        "host": "github.com",
        "installation_id": 7,
        "not_before": None,
        "reason": "customer request",
        "to_state": "requested",
    }
    assert "acme" not in json.dumps(event.after)  # never the organisation login
    assert world.written == []


def test_the_server_computes_not_before_from_its_grace_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured window sets `not_before`; none (the default) leaves it null."""
    world = World()
    before = datetime.now(UTC)
    assert (
        _client(monkeypatch, world, ADMIN, grace=3600).post(REQUEST, json=BODY).status_code == 202
    )
    (params,) = world.ran("tb_request_tenant_deletion")
    assert timedelta(seconds=3599) < params["not_before"] - before < timedelta(seconds=3605)


def test_not_before_is_not_settable_by_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    """The body forbids unknown fields (a 422), so a caller cannot choose the window."""
    world = World()
    client = _client(monkeypatch, world, ADMIN)
    stamp = "2026-12-01T00:00:00Z"
    problem(client.post(REQUEST, json={**BODY, "not_before": stamp}), 422, "validation")
    assert world.statements == []


@pytest.mark.parametrize(
    "change",
    [
        {"host": ""},
        {"installation_id": 0},
        {"installation_id": 2**63},
        {"organization": ""},
        {"organization": "bad\tlogin"},
        {"host": "bad\nhost"},
        {"reason": ""},
        {"reason": "x" * 501},
        {"delete_platform_resources": "maybe"},
        {"extra": 1},
    ],
)
def test_a_bad_request_body_is_a_422_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, Any]
) -> None:
    """Each field is bounded; nothing reaches the database."""
    world = World()
    problem(_client(monkeypatch, world, ADMIN).post(REQUEST, json={**BODY, **change}), 422)
    assert world.statements == []


@pytest.mark.parametrize("missing", ["host", "installation_id", "organization", "reason"])
def test_the_confirmation_and_reason_are_required(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """No default for the typed confirmation."""
    world = World()
    body = {k: v for k, v in BODY.items() if k != missing}
    problem(_client(monkeypatch, world, ADMIN).post(REQUEST, json=body), 422, "validation")
    assert world.statements == []


def test_a_repeat_request_returns_the_open_record_and_audits_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idempotent per tenant: the same 202, an event whose `before` is the record."""
    world = World()
    world.request = SimpleNamespace(outcome="existing", deletion_id=DELETION)
    world.record = _record(state="platform", delete_platform_resources=True)
    response = _client(monkeypatch, world, ADMIN).post(REQUEST, json=BODY)
    assert response.status_code == 202
    assert response.json()["state"] == "platform"
    assert response.json()["delete_platform_resources"] is True  # the first request's flag
    (event,) = world.events
    assert event.before is not None
    assert event.before["deletion_id"] == str(DELETION)
    assert event.after is not None
    assert event.after["to_state"] == "platform"
    assert event.after["delete_platform_resources"] is True


@pytest.mark.parametrize(
    ("outcome", "status", "kind", "reason"),
    [
        ("not_found", 404, "not-found", "not_found"),
        ("not_disabled", 409, "conflict", "conflict"),
        ("confirmation_mismatch", 409, "conflict", "conflict"),
    ],
)
def test_a_refused_request_is_a_problem_with_a_failure_event(
    monkeypatch: pytest.MonkeyPatch, outcome: str, status: int, kind: str, reason: str
) -> None:
    """A refusal writes nothing but a `failure` event for the tenant, with a fixed reason."""
    world = World()
    world.request = SimpleNamespace(outcome=outcome, deletion_id=None)
    response = _client(monkeypatch, world, ADMIN).post(REQUEST, json=BODY)
    assert "acme" not in problem(response, status, kind)["detail"]
    assert world.events == []
    (event,) = world.written
    assert (event.action, event.outcome, event.tenant_id) == (
        Action.TENANT_DELETION_REQUESTED,
        Outcome.FAILURE,
        TENANT,
    )
    assert event.after == {"reason": reason}


def test_a_record_that_cannot_be_read_back_after_the_request_is_a_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read in the transaction that wrote it: unreachable, but never a 500 or a bare answer."""
    world = World()
    world.record = None
    problem(_client(monkeypatch, world, ADMIN).post(REQUEST, json=BODY), 404, "not-found")
    assert world.events == []
    assert [e.after for e in world.written] == [{"reason": "not_found"}]


def test_a_secret_shaped_reason_is_masked_in_the_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reason is recorded no more openly than the audit log records anything."""
    world = World()
    secret = f"cleanup token={CANARY} done"
    response = _client(monkeypatch, world, ADMIN).post(REQUEST, json={**BODY, "reason": secret})
    assert response.status_code == 202
    assert all(CANARY not in repr(e) for e in world.events)
    assert CANARY not in response.text


def test_a_database_error_on_request_is_a_500_with_an_internal_failure_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The error is not hidden and the event carries the fixed code, never the text."""
    world = World()
    world.request = None  # type: ignore[assignment]
    client = _client(monkeypatch, world, ADMIN, server_errors=False)
    problem(client.post(REQUEST, json=BODY), 500, "internal")
    (event,) = world.written
    assert event.after == {"reason": "internal"}


def test_a_request_without_a_database_is_a_503() -> None:
    """No session factory: 503 for every deletion route."""
    env = build(factory=None)
    problem(env.client(ADMIN).post(REQUEST, json=BODY), 503, "unavailable")
    problem(env.client(VIEWER).get(BASE), 503, "unavailable")
    problem(env.client(ADMIN).post(f"{BASE}/cancel"), 503, "unavailable")


# ---- reads -------------------------------------------------------------------------------


def test_one_deletion_is_shown_without_lease_holder_fence_or_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proposed view, nothing else; the failure is a code, never its message."""
    world = World()
    world.record = _record(
        state="github",
        counts={"adapter_resources": 3, "bad key": 1, "negative": -1},
        skipped=[
            {"category": "github_outputs", "reason": "unreachable", "repository_ids": [1, 2]},
            {"category": "x", "secret_value": "hidden", "note": f"token={CANARY}"},
        ],
        attempts=2,
        last_error=f"GithubError: could not reach {CANARY}",
        not_before=AT,
        holder="reconciler-1",
        fence=9,
        step_cursor={"table": "x"},
    )
    response = _client(monkeypatch, world, VIEWER).get(BASE)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "id",
        "tenant_id",
        "host",
        "installation_id",
        "state",
        "delete_platform_resources",
        "not_before",
        "requested_at",
        "requested_by",
        "counts",
        "skipped",
        "attempts",
        "last_error_code",
        "updated_at",
        "hint",
    }
    assert body["counts"] == {"adapter_resources": 3}
    assert body["skipped"][0] == {
        "category": "github_outputs",
        "reason": "unreachable",
        "repository_ids": [1, 2],
    }
    assert "secret_value" not in body["skipped"][1]
    assert body["last_error_code"] == "GithubError"
    assert (body["requested_by"], body["attempts"]) == ("u1", 2)
    assert CANARY not in response.text
    assert "reconciler-1" not in response.text


def test_an_unknown_deletion_is_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """Also an id that is not a UUID is a 422."""
    world = World()
    world.record = None
    client = _client(monkeypatch, world, VIEWER)
    problem(client.get(BASE), 404, "not-found")
    problem(client.get("/admin/v1/tenant-deletions/not-a-uuid"), 422, "validation")


def test_the_list_pages_newest_first_with_a_keyset_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One extra row decides the next cursor, which names the last row sent."""
    world = World()
    older = AT - timedelta(days=1)
    world.records = [
        _record(id=UUID(int=3), requested_at=AT),
        _record(id=UUID(int=2), requested_at=older),
        _record(id=UUID(int=1), requested_at=older - timedelta(days=1)),
    ]
    client = _client(monkeypatch, world, VIEWER)
    response = client.get("/admin/v1/tenant-deletions", params={"limit": 2})
    assert response.status_code == 200
    page = response.json()
    assert [i["id"] for i in page["items"]] == [str(UUID(int=3)), str(UUID(int=2))]
    assert decode_cursor(page["next_cursor"]) == (older.isoformat(), str(UUID(int=2)))
    first = world.ran("ORDER BY requested_at DESC")[0]
    assert (first["since"], first["limit"]) == (None, 3)
    world.records = [_record(id=UUID(int=1))]
    last = client.get(
        "/admin/v1/tenant-deletions", params={"limit": 2, "cursor": page["next_cursor"]}
    ).json()
    assert last["next_cursor"] is None
    resumed = world.ran("ORDER BY requested_at DESC")[1]
    assert (resumed["since"], resumed["id"]) == (older, UUID(int=2))
    problem(client.get("/admin/v1/tenant-deletions", params={"cursor": "!!"}), 400)


# ---- cancel ------------------------------------------------------------------------------


def test_cancel_audits_in_the_deletions_tenant_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """The record is outside RLS but its event is not: the transaction runs for the tenant."""
    world = World()
    world.record = _record(state="requested")
    response = _client(monkeypatch, world, ADMIN).post(f"{BASE}/cancel")
    assert response.status_code == 200, response.text
    assert world.ran("tb_cancel_tenant_deletion") == [{"id": DELETION}]
    assert world.contexts == [TENANT]
    (event,) = world.events
    assert (event.action, event.outcome, event.tenant_id) == (
        Action.TENANT_DELETION_CANCELLED,
        Outcome.SUCCESS,
        TENANT,
    )
    assert (event.resource_type, event.resource_id) == ("tenant_deletion", str(DELETION))
    assert event.before == {"deletion_id": str(DELETION), "from_state": "requested"}
    assert event.after == {
        "deletion_id": str(DELETION),
        "from_state": "requested",
        "to_state": "cancelled",
    }


def test_cancel_after_the_first_step_is_a_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """There is no cancel once a destructive step ran."""
    world = World()
    world.cancel = "not_cancellable"
    problem(_client(monkeypatch, world, ADMIN).post(f"{BASE}/cancel"), 409, "conflict")
    assert world.events == []
    (event,) = world.written
    assert (event.action, event.outcome, event.tenant_id) == (
        Action.TENANT_DELETION_CANCELLED,
        Outcome.FAILURE,
        TENANT,
    )
    assert event.after == {"reason": "conflict"}


@pytest.mark.parametrize("suffix", ["cancel", "skip-step", "fail"])
def test_an_unknown_deletion_is_a_404_with_a_tenantless_failure_event(
    monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    """No record, no tenant: the failure event has none and names the id asked about."""
    world = World()
    world.record = None
    client = _client(monkeypatch, world, ADMIN)
    body = None if suffix == "cancel" else {"reason": "x"}
    problem(client.post(f"{BASE}/{suffix}", json=body), 404, "not-found")
    assert world.contexts == []
    (event,) = world.written
    assert (event.tenant_id, event.resource_id, event.outcome) == (
        None,
        str(DELETION),
        Outcome.FAILURE,
    )
    assert event.after == {"reason": "not_found"}


def test_a_record_purged_between_the_two_reads_is_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first read finds it, the transaction does not."""
    world = World()
    reads = iter([_record(), None])
    factory = world.factory()

    class Session(factory):  # type: ignore[valid-type, misc]
        async def execute(self, statement: object, params: dict[str, Any] | None = None) -> Any:  # noqa: ANN401
            if "WHERE id = :id" in str(statement):
                found = next(reads)
                return Result([] if found is None else [found])
            return await super().execute(statement, params)

    env = build(factory=Session)
    env.app.state.admin = replace(env.runtime, writer=Writer(world))  # type: ignore[arg-type]

    async def append(session: object, event: AuditEvent) -> UUID:
        del session
        return event.id

    monkeypatch.setattr(routes, "append", append)
    problem(env.client(ADMIN).post(f"{BASE}/cancel"), 404, "not-found")
    assert [e.after for e in world.written] == [{"reason": "not_found"}]


def test_a_record_that_vanishes_after_the_write_is_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """The final read, in the transaction that wrote, finds nothing: rolled back and reported."""
    world = World()
    reads = iter([_record(), _record(), None])
    factory = world.factory()

    class Session(factory):  # type: ignore[valid-type, misc]
        async def execute(self, statement: object, params: dict[str, Any] | None = None) -> Any:  # noqa: ANN401
            if "WHERE id = :id" in str(statement):
                found = next(reads)
                return Result([] if found is None else [found])
            return await super().execute(statement, params)

    env = build(factory=Session)
    env.app.state.admin = replace(env.runtime, writer=Writer(world))  # type: ignore[arg-type]

    async def append(session: object, event: AuditEvent) -> UUID:
        del session
        return event.id

    monkeypatch.setattr(routes, "append", append)
    problem(env.client(ADMIN).post(f"{BASE}/cancel"), 404, "not-found")
    assert [e.after for e in world.written] == [{"reason": "not_found"}]


def test_the_cancel_function_losing_the_record_is_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """`not_found` from the definer (purged after the reads)."""
    world = World()
    world.cancel = "not_found"
    problem(_client(monkeypatch, world, ADMIN).post(f"{BASE}/cancel"), 404, "not-found")


# ---- skip-step ---------------------------------------------------------------------------


def test_skip_step_records_who_acknowledged_it_and_audits(monkeypatch: pytest.MonkeyPatch) -> None:
    """The function gets the caller's `sub`; the event has the step and the scrubbed reason."""
    world = World()
    world.record = _record(state="platform", attempts=3)
    response = _client(monkeypatch, world, ADMIN).post(
        f"{BASE}/skip-step", json={"reason": f"harbor is gone token={CANARY}"}
    )
    assert response.status_code == 200, response.text
    assert world.ran("tb_skip_tenant_deletion_step") == [{"id": DELETION, "actor": "u1"}]
    assert world.contexts == [TENANT]
    (event,) = world.events
    assert (event.action, event.outcome, event.tenant_id) == (
        Action.TENANT_DELETION_STEP_SKIPPED,
        Outcome.SUCCESS,
        TENANT,
    )
    assert event.after is not None
    assert (event.after["step"], event.after["from_state"], event.after["to_state"]) == (
        "platform",
        "platform",
        "github",
    )
    assert "harbor is gone" in event.after["reason"]
    assert CANARY not in repr(event)


@pytest.mark.parametrize(
    ("outcome", "status"),
    [
        ("terminal", 409),
        ("not_skippable", 409),
        ("not_failed", 409),
        ("lease_held", 409),
        ("not_found", 404),
    ],
)
def test_a_refused_skip_is_a_problem_with_its_own_detail(
    monkeypatch: pytest.MonkeyPatch, outcome: str, status: int
) -> None:
    """Each refusal says why, and leaves a `failure` event for the tenant."""
    world = World()
    world.skip = SimpleNamespace(outcome=outcome, from_state="draining", to_state=None)
    body = problem(
        _client(monkeypatch, world, ADMIN).post(f"{BASE}/skip-step", json={"reason": "x"}),
        status,
    )
    if status == 409:
        assert body["detail"] == deletions.SKIP_REFUSALS[outcome]
    assert world.events == []
    (event,) = world.written
    assert (event.action, event.outcome) == (
        Action.TENANT_DELETION_STEP_SKIPPED,
        Outcome.FAILURE,
    )


def test_the_skip_refusals_cover_every_outcome_the_function_has() -> None:
    """The four non-skip outcomes of `tb_skip_tenant_deletion_step` besides `not_found`."""
    assert set(deletions.SKIP_REFUSALS) == {"terminal", "not_skippable", "not_failed", "lease_held"}


@pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "x", "step": "github"}])
def test_skip_and_fail_need_exactly_a_reason(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    """`{reason}` and nothing else."""
    world = World()
    client = _client(monkeypatch, world, ADMIN)
    problem(client.post(f"{BASE}/skip-step", json=body), 422, "validation")
    problem(client.post(f"{BASE}/fail", json=body), 422, "validation")
    assert world.statements == []


# ---- operator fail -----------------------------------------------------------------------


def test_fail_ends_the_deletion_and_audits_tenant_deletion_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The function gets the actor and the scrubbed reason; the event is the operator's."""
    world = World()
    world.record = _record(state="github", attempts=5)
    response = _client(monkeypatch, world, ADMIN).post(
        f"{BASE}/fail", json={"reason": "stuck for a week"}
    )
    assert response.status_code == 200, response.text
    assert world.ran("tb_operator_fail_tenant_deletion") == [
        {"id": DELETION, "actor": "u1", "reason": "stuck for a week"}
    ]
    assert world.contexts == [TENANT]
    (event,) = world.events
    assert (event.action, event.outcome, event.tenant_id) == (
        Action.TENANT_DELETION_OPERATOR_FAILED,
        Outcome.SUCCESS,
        TENANT,
    )
    assert event.action.value == "tenant.deletion_failed"
    assert event.before == {"deletion_id": str(DELETION), "from_state": "github"}
    assert event.after == {
        "deletion_id": str(DELETION),
        "from_state": "github",
        "reason": "stuck for a week",
        "step": "github",
        "to_state": "failed",
    }


@pytest.mark.parametrize("sqlstate", ["TB003", "TB004"])
def test_a_held_lease_or_an_ended_deletion_is_a_409(
    monkeypatch: pytest.MonkeyPatch, sqlstate: str
) -> None:
    """The definer's two refusals become a conflict, with a failure event."""
    world = World()
    world.error = _db_error(sqlstate)
    problem(
        _client(monkeypatch, world, ADMIN).post(f"{BASE}/fail", json={"reason": "x"}),
        409,
        "conflict",
    )
    assert world.events == []
    (event,) = world.written
    assert (event.action, event.after) == (
        Action.TENANT_DELETION_OPERATOR_FAILED,
        {"reason": "conflict"},
    )


def test_any_other_database_error_is_a_500_with_an_internal_failure_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not mapped, not hidden, and the text stays out of the event."""
    world = World()
    world.error = _db_error("XX000")
    client = _client(monkeypatch, world, ADMIN, server_errors=False)
    problem(client.post(f"{BASE}/fail", json={"reason": "x"}), 500, "internal")
    (event,) = world.written
    assert event.after == {"reason": "internal"}


def test_a_failure_event_that_cannot_be_written_does_not_hide_the_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best effort only; and with no writer at all there is nothing to record."""
    world = World()
    world.cancel = "not_cancellable"
    problem(
        _client(monkeypatch, world, ADMIN, writer=OSError("audit down")).post(f"{BASE}/cancel"),
        409,
    )
    problem(_client(monkeypatch, world, ADMIN, writer=False).post(f"{BASE}/cancel"), 409)
    assert world.written == []


# ---- the pure parts ----------------------------------------------------------------------


def test_not_before_is_none_without_a_window_and_now_plus_it_with_one() -> None:
    """The server's grace window, never the caller's."""
    assert deletions.not_before(0) is None
    assert deletions.not_before(-5) is None
    assert deletions.not_before(90, now=AT) == AT + timedelta(seconds=90)
    assert deletions.not_before(90) is not None


@pytest.mark.parametrize(
    ("stored", "code"),
    [
        (None, None),
        ("", None),
        ("GithubError: could not reach host", "GithubError"),
        ("invalid_step_output: bad count", "invalid_step_output"),
        ("TimeoutError", "TimeoutError"),
        ("operator u1: stuck", "operator_failed"),
        ("something went wrong: x", "error"),
        ("9bad: x", "error"),
        (": x", "error"),
        ("ghp_AbCdEfGh", "error"),
        ("ghs_AbC123", "error"),
        ("KeyError: 'x'", "KeyError"),
        ("KeyboardInterrupt", "KeyboardInterrupt"),
        ("token_expired: x", "token_expired"),
        ("httpx.ConnectError: down", "httpx.ConnectError"),
    ],
)
def test_the_error_code_is_a_short_identifier_never_the_message(
    stored: str | None, code: str | None
) -> None:
    """Class or fixed code only; operator failures are one code."""
    assert views.error_code(stored) == code


def test_the_skipped_view_keeps_short_safe_entries_only() -> None:
    """Scalars, scrubbed strings and number lists; keys that name a secret or are long go."""
    entries = [
        {
            "category": "c",
            "count": 2,
            "flag": True,
            "ids": [1, True, "x", 2],
            "nested": {"a": 1},
            "none": None,
            "api_key": "k",
            "long" * 40: 1,
            "bad key": 1,
            5: 1,
            "text": f"token={CANARY}",
        },
        "not a mapping",
    ]
    (kept,) = views.skipped_entries(entries)
    assert kept["category"] == "c"
    assert (kept["count"], kept["flag"], kept["ids"]) == (2, True, [1, 2])
    assert not {"nested", "none", "api_key", "bad key", 5} & kept.keys()
    assert all(len(str(k)) <= 64 for k in kept)
    assert CANARY not in repr(kept)
    assert views.skipped_entries("text") == []
    assert views.skipped_entries(None) == []
    assert views.deletion_counts_view(None) == {}


def test_a_deletion_id_is_a_uuid_in_the_events() -> None:
    """`state_of` accepts the record's types and records only scalars."""
    record = SimpleNamespace(
        id=uuid4(), host="h", installation_id=1, delete_platform_resources=False, not_before=AT
    )
    shown = deletions.state_of(record, to_state="requested")  # type: ignore[arg-type]
    assert shown["not_before"] == "2026-11-16T09:00:00Z"
    assert shown["to_state"] == "requested"
