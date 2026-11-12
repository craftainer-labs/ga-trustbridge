"""Delivery requeue and tenant reconcile over fakes (ADR 0033, NG6e-3)."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from tests.trustbridge.admin.helpers import ACME, OTHER, FakeSession, build, problem
from trustbridge.admin import guards, queries, routes
from trustbridge.admin.problems import AdminProblem
from trustbridge.admin.schemas import DeliveryDetailOut
from trustbridge.audit import Action, AuditEvent, AuditWriter, Outcome
from trustbridge.rbac import Role

OPERATOR = "trustbridge:operator"
VIEWER = "trustbridge:viewer"
ACME_OPERATOR = "trustbridge:github.com:acme:operator"
OTHER_OPERATOR = "trustbridge:github.com:other:operator"
AT = datetime(2026, 11, 8, 9, 0, tzinfo=UTC)
CANARY = "canary-secret-7f3a9c1e"
REQUEUE = "/admin/v1/deliveries/github.com/abc-123/requeue"
RECONCILE = f"/admin/v1/tenants/{ACME.id}/reconcile"


def _detail(status: str = "pending") -> DeliveryDetailOut:
    return DeliveryDetailOut(
        host="github.com",
        delivery_id="abc-123",
        event="push",
        status=status,
        attempts=0,
        received_at=AT,
        next_attempt_at=None,
        last_relayed_at=None,
        processed_at=None,
        dead_at=None,
        last_error=None,
        body_sha256=None,
        has_body=True,
        payload={"action": "opened"},
    )


class World:
    """Fakes for the database calls the two routes make."""

    def __init__(self) -> None:
        """A dead delivery that gets requeued, and an enabled tenant."""
        self.requeue: queries.Requeue | None = queries.Requeue(
            {"status": "dead", "attempts": 8, "dead_at": AT, "last_error": f"token={CANARY}"},
            {"status": "pending", "attempts": 0, "dead_at": None, "last_error": None},
            changed=True,
        )
        self.requeue_error: Exception | None = None
        self.detail: DeliveryDetailOut | None = _detail()
        self.tenant_row: tuple[Any, ...] | None = _tenant_row(False)
        self.events: list[AuditEvent] = []
        self.written: list[AuditEvent] = []
        self.payload_flags: list[bool] = []
        self.scheduled: list[UUID] = []
        self.schedule_error: Exception | None = None

    def session(self) -> Any:  # noqa: ANN401
        """A session factory whose `execute` answers the tenant lookup."""
        world = self

        class Session(FakeSession):
            async def execute(self, statement: object, params: dict[str, Any]) -> Any:  # noqa: ANN401
                del statement, params
                return SimpleNamespace(first=lambda: world.tenant_row)

        return Session


def _tenant_row(disabled: bool) -> tuple[Any, ...]:
    return (
        ACME.id,
        "github.com",
        7,
        "acme",
        70,
        "active",
        AT if disabled else None,
        False,
        "u1" if disabled else None,
        "hold" if disabled else None,
    )


class Scheduler:
    """A `ReconcileScheduler` that records."""

    def __init__(self, world: World) -> None:
        """Record into `world`."""
        self.world = world

    async def schedule(self, tenant_id: UUID) -> None:
        """Record the request or raise."""
        if self.world.schedule_error is not None:
            raise self.world.schedule_error
        self.world.scheduled.append(tenant_id)


class Writer(AuditWriter):
    """An `AuditWriter` that records instead of writing."""

    def __init__(self, world: World) -> None:
        """Record into `world.written`."""
        self.world = world

    async def record(self, event: AuditEvent) -> UUID:
        """Record the event."""
        self.world.written.append(event)
        return event.id


def _client(
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    *roles: str,
    scheduler: bool = True,
    server_errors: bool = True,
) -> TestClient:
    env = build(factory=world.session(), scheduler=Scheduler(world) if scheduler else None)
    env.app.state.admin = replace(env.runtime, writer=Writer(world))

    async def requeue_delivery(session: object, host: str, delivery_id: str) -> Any:  # noqa: ANN401
        del session, host, delivery_id
        if world.requeue_error is not None:
            raise world.requeue_error
        return world.requeue

    async def delivery(
        session: object, host: str, delivery_id: str, *, with_payload: bool
    ) -> DeliveryDetailOut | None:
        del session, host, delivery_id
        world.payload_flags.append(with_payload)
        return world.detail

    async def append(session: object, event: AuditEvent) -> UUID:
        del session
        world.events.append(event)
        return event.id

    monkeypatch.setattr(queries, "requeue_delivery", requeue_delivery)
    monkeypatch.setattr(queries, "delivery", delivery)
    monkeypatch.setattr(routes, "append", append)
    client = env.client(*roles)
    if not server_errors:
        client = TestClient(
            env.app,
            client=("testclient", 50000),
            raise_server_exceptions=False,
            headers=dict(client.headers),
        )
    return client


def test_both_routes_are_guarded_emit_and_use_the_mutation_bucket() -> None:
    """GUARDED has the operator guard (the tenant one with `tenant_id`); the walk accepts them."""
    api = [r for r in routes.router.routes if isinstance(r, APIRoute)]
    expected = {
        routes.requeue_delivery: (Role.OPERATOR, None, False),
        routes.reconcile_tenant: (Role.OPERATOR, "tenant_id", False),
    }
    for endpoint, entry in expected.items():
        (route,) = [r for r in api if r.endpoint is endpoint]
        entries = [
            guards.GUARDED[d.call]
            for d in route.dependant.dependencies
            if d.call in guards.GUARDED and guards.GUARDED[d.call][0] is not None
        ]
        assert entries == [entry]
    assert guards.EMITTERS[routes.requeue_delivery] is Action.DELIVERY_REQUEUE
    assert guards.EMITTERS[routes.reconcile_tenant] is Action.TENANT_RECONCILE
    assert guards.mutations_without_emitter(routes.router.routes) == []


def test_requeue_resets_the_delivery_and_audits_it_with_the_scrubbed_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One success event: instance scope (no tenant), `host/id`, before and after."""
    world = World()
    response = _client(monkeypatch, world, OPERATOR).post(REQUEUE)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["payload"]) == ("pending", {"action": "opened"})
    assert "headers" not in body
    assert world.payload_flags == [True]
    (event,) = world.events
    assert (event.action, event.outcome, event.tenant_id) == (
        Action.DELIVERY_REQUEUE,
        Outcome.SUCCESS,
        None,
    )
    assert (event.resource_type, event.resource_id) == ("delivery", "github.com/abc-123")
    assert event.before is not None
    assert (event.before["status"], event.before["attempts"]) == ("dead", 8)
    assert CANARY not in repr(event.before)
    assert event.after == {"attempts": 0, "dead_at": None, "last_error": None, "status": "pending"}
    assert world.written == []


def test_requeueing_a_pending_delivery_is_a_success_with_before_equal_to_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-op still writes its event."""
    world = World()
    state = {"status": "pending", "attempts": 2, "dead_at": None, "last_error": None}
    world.requeue = queries.Requeue(state, state, changed=False)
    assert _client(monkeypatch, world, OPERATOR).post(REQUEUE).status_code == 200
    (event,) = world.events
    assert event.before == event.after


def test_requeue_of_an_unknown_delivery_is_a_404_and_a_conflict_is_a_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither writes an event."""
    world = World()
    world.requeue = None
    problem(_client(monkeypatch, world, OPERATOR).post(REQUEUE), 404, "not-found")
    world.requeue_error = AdminProblem(409, "conflict", "Only a dead delivery can be requeued")
    problem(_client(monkeypatch, world, OPERATOR).post(REQUEUE), 409, "conflict")
    assert world.events == []
    assert world.written == []


def test_a_delivery_that_vanishes_after_the_reset_is_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """The final read finds nothing."""
    world = World()
    world.detail = None
    problem(_client(monkeypatch, world, OPERATOR).post(REQUEUE), 404, "not-found")


def test_requeue_paths_are_bounded_and_the_role_is_operator_at_instance_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """422 for an over-long host or id; viewers and tenant-scoped operators get 403."""
    world = World()
    client = _client(monkeypatch, world, OPERATOR)
    problem(client.post(f"/admin/v1/deliveries/{'h' * 64}/x/requeue"), 422, "validation")
    problem(client.post(f"/admin/v1/deliveries/github.com/{'d' * 129}/requeue"), 422, "validation")
    for role in (VIEWER, ACME_OPERATOR):
        problem(_client(monkeypatch, world, role).post(REQUEUE), 403, "forbidden")
    assert world.events == []


def test_a_requeue_failure_leaves_a_failure_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """The error propagates; the failure event is its own record."""
    world = World()
    world.requeue_error = RuntimeError("db down")
    problem(_client(monkeypatch, world, OPERATOR, server_errors=False).post(REQUEUE), 500)
    (failed,) = world.written
    assert (failed.action, failed.outcome, failed.resource_id) == (
        Action.DELIVERY_REQUEUE,
        Outcome.FAILURE,
        "github.com/abc-123",
    )
    assert failed.after == {"reason": "internal"}
    assert world.events == []


def test_reconcile_records_the_request_and_audits_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """202 with `requested`, one `tenant.reconcile` event without before or after."""
    world = World()
    response = _client(monkeypatch, world, OPERATOR).post(RECONCILE)
    assert (response.status_code, response.json()) == (202, {"requested": True})
    assert world.scheduled == [ACME.id]
    (event,) = world.events
    assert (event.action, event.outcome, event.tenant_id) == (
        Action.TENANT_RECONCILE,
        Outcome.SUCCESS,
        ACME.id,
    )
    assert (event.before, event.after) == (None, None)


def test_a_repeat_reconcile_is_accepted_and_audited_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """The set makes it one request; each call is still one event."""
    world = World()
    client = _client(monkeypatch, world, ACME_OPERATOR)
    assert client.post(RECONCILE).status_code == 202
    assert client.post(RECONCILE).status_code == 202
    assert len(world.events) == 2


def test_reconcile_of_a_disabled_or_missing_tenant_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """409 for a disabled tenant, 404 when the row is gone; no request, no event."""
    world = World()
    world.tenant_row = _tenant_row(True)
    problem(_client(monkeypatch, world, OPERATOR).post(RECONCILE), 409, "conflict")
    world.tenant_row = None
    problem(_client(monkeypatch, world, OPERATOR).post(RECONCILE), 404, "not-found")
    assert (world.scheduled, world.events) == ([], [])


def test_reconcile_roles_and_missing_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    """A viewer is 403, an operator of another tenant 404, and no queue is a 503."""
    world = World()
    problem(_client(monkeypatch, world, VIEWER).post(RECONCILE), 403, "forbidden")
    problem(_client(monkeypatch, world, OTHER_OPERATOR).post(RECONCILE), 404, "not-found")
    problem(_client(monkeypatch, world, OPERATOR, scheduler=False).post(RECONCILE), 503)
    assert OTHER.id != ACME.id
    assert world.events == []


def test_a_reconcile_that_cannot_be_recorded_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The queue is down: 500, a failure event, nothing scheduled."""
    world = World()
    world.schedule_error = OSError("valkey down")
    problem(_client(monkeypatch, world, OPERATOR, server_errors=False).post(RECONCILE), 500)
    (failed,) = world.written
    assert (failed.action, failed.outcome) == (Action.TENANT_RECONCILE, Outcome.FAILURE)
    assert failed.after == {"reason": "internal"}
    assert world.scheduled == []
