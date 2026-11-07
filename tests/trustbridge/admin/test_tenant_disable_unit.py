"""Tenant disable and enable over fakes (ADR 0033 "Tenant disable", invariant 19)."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from tests.trustbridge.admin.helpers import ACME, FakeSession, build, problem
from trustbridge.admin import guards, routes
from trustbridge.audit import Action, AuditEvent, Outcome
from trustbridge.rbac import Role
from trustbridge.tenancy import TenantInfo

ADMIN = "trustbridge:admin"
OPERATOR = "trustbridge:operator"
ACME_ADMIN = "trustbridge:github.com:acme:admin"
AT = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
CANARY = "canary-secret-7f3a9c1e"
DISABLE = f"/admin/v1/tenants/{ACME.id}/disable"
ENABLE = f"/admin/v1/tenants/{ACME.id}/enable"


def _row(**changes: Any) -> SimpleNamespace:  # noqa: ANN401 -- a result row
    values: dict[str, Any] = {
        "tenant_found": True,
        "before_disabled_at": None,
        "before_disabled_by": None,
        "before_disable_reason": None,
        "after_disabled_at": AT,
        "after_disabled_by": "u1",
        "after_disable_reason": "billing hold",
    }
    values.update(changes)
    return SimpleNamespace(**values)


class World:
    """A fake database: records what the routes execute and append."""

    def __init__(self, row: SimpleNamespace | None = None, error: Exception | None = None) -> None:
        """The row `tb_set_tenant_disabled` returns, or the error it raises."""
        self.row = row or _row()
        self.error = error
        self.params: list[dict[str, Any]] = []
        self.events: list[AuditEvent] = []
        self.written: list[AuditEvent] = []
        self.info: TenantInfo | None = _info()

    def factory(self) -> Any:  # noqa: ANN401
        """A session factory whose sessions return `row`."""
        world = self

        class Session(FakeSession):
            async def execute(self, statement: object, params: dict[str, Any]) -> Any:  # noqa: ANN401
                del statement
                world.params.append(params)
                if world.error is not None:
                    raise world.error
                return SimpleNamespace(one=lambda: world.row)

        return Session


def _info(disabled: bool = True) -> TenantInfo:
    return TenantInfo(
        ACME.id,
        "github.com",
        7,
        "acme",
        70,
        "active",
        AT if disabled else None,
        False,
        "u1" if disabled else None,
        "billing hold" if disabled else None,
    )


class Writer:
    """An `AuditWriter` stand-in."""

    def __init__(self, world: World, error: Exception | None = None) -> None:
        """Record into `world.written`."""
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
    writer: bool | Exception = True,
    server_errors: bool = True,
) -> TestClient:
    env = build(factory=world.factory())
    if writer is not False:
        error = writer if isinstance(writer, Exception) else None
        env.app.state.admin = replace(env.runtime, writer=Writer(world, error))  # type: ignore[arg-type]

    async def append(session: object, event: AuditEvent) -> UUID:
        del session
        world.events.append(event)
        return event.id

    async def get_tenant(factory: object, tenant_id: UUID) -> TenantInfo | None:
        del factory, tenant_id
        return world.info

    monkeypatch.setattr(routes, "append", append)
    monkeypatch.setattr(routes, "get_tenant", get_tenant)
    client = env.client(*roles)
    if not server_errors:
        client = TestClient(
            env.app,
            client=("testclient", 50000),
            raise_server_exceptions=False,
            headers=dict(client.headers),
        )
    return client


def test_both_routes_need_instance_scope_admin_and_the_mutation_bucket() -> None:
    """GUARDED records `instance_only` for the two routes; the walk accepts them."""
    api = [r for r in routes.router.routes if isinstance(r, APIRoute)]
    for endpoint in (routes.disable_tenant, routes.enable_tenant):
        (route,) = [r for r in api if r.endpoint is endpoint]
        entries = [
            guards.GUARDED[d.call]
            for d in route.dependant.dependencies
            if d.call in guards.GUARDED and guards.GUARDED[d.call][0] is not None
        ]
        assert entries == [(Role.ADMIN, "tenant_id", True)]
    assert guards.EMITTERS[routes.disable_tenant] is Action.TENANT_DISABLE
    assert guards.EMITTERS[routes.enable_tenant] is Action.TENANT_ENABLE
    assert guards.mutations_without_emitter(routes.router.routes) == []


def test_disable_sets_the_columns_audits_in_the_same_session_and_returns_the_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One call: the write, one `tenant.disable` success event with before and after."""
    world = World()
    response = _client(monkeypatch, world, ADMIN).post(DISABLE, json={"reason": "billing hold"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["effective_state"], body["admin_disabled_by"]) == ("disabled", "u1")
    assert body["admin_disable_reason"] == "billing hold"
    assert world.params == [{"id": ACME.id, "by": "u1", "reason": "billing hold", "disable": True}]
    (event,) = world.events
    assert (event.action, event.outcome, event.tenant_id) == (
        Action.TENANT_DISABLE,
        Outcome.SUCCESS,
        ACME.id,
    )
    assert (event.resource_type, event.resource_id) == ("tenant", str(ACME.id))
    assert (event.actor_sub, event.actor_iss) == ("u1", "https://idp.example")
    assert event.before == {
        "admin_disable_reason": None,
        "admin_disabled_at": None,
        "admin_disabled_by": None,
    }
    assert event.after == {
        "admin_disable_reason": "billing hold",
        "admin_disabled_at": "2026-11-07T09:00:00Z",
        "admin_disabled_by": "u1",
    }
    assert world.written == []


def test_a_repeat_disable_is_a_success_with_the_original_actor_and_before_equal_to_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The function keeps the first time, actor and reason; the event still exists."""
    kept = _row(
        before_disabled_at=AT,
        before_disabled_by="first",
        before_disable_reason="hold",
        after_disabled_by="first",
        after_disable_reason="hold",
    )
    world = World(kept)
    response = _client(monkeypatch, world, ADMIN).post(DISABLE, json={"reason": "again"})
    assert response.status_code == 200
    (event,) = world.events
    assert event.outcome is Outcome.SUCCESS
    assert event.before == event.after
    assert event.after is not None
    assert event.after["admin_disabled_by"] == "first"


def test_enable_clears_the_columns_and_audits_tenant_enable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enable takes no body; the event has the cleared values as `after`."""
    world = World(
        _row(
            before_disabled_at=AT,
            before_disabled_by="u0",
            before_disable_reason="hold",
            after_disabled_at=None,
            after_disabled_by=None,
            after_disable_reason=None,
        )
    )
    world.info = _info(disabled=False)
    response = _client(monkeypatch, world, ADMIN).post(ENABLE)
    assert response.status_code == 200
    assert response.json()["admin_disabled_at"] is None
    assert world.params == [{"id": ACME.id, "by": "u1", "reason": "", "disable": False}]
    (event,) = world.events
    assert event.action is Action.TENANT_ENABLE
    assert event.after == {
        "admin_disable_reason": None,
        "admin_disabled_at": None,
        "admin_disabled_by": None,
    }


def test_enabling_an_enabled_tenant_is_a_success_with_before_equal_to_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-op still writes its event."""
    nothing = _row(after_disabled_at=None, after_disabled_by=None, after_disable_reason=None)
    world = World(nothing)
    world.info = _info(disabled=False)
    assert _client(monkeypatch, world, ADMIN).post(ENABLE).status_code == 200
    (event,) = world.events
    assert event.before == event.after


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"reason": ""},
        {"reason": "   "},
        {"reason": "x" * 501},
        {"reason": "tab\tseparated"},
        {"reason": "ok", "extra": 1},
        {"reason": 5},
    ],
)
def test_a_bad_reason_is_a_422_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    """1 to 500 printable characters, nothing else in the body."""
    world = World()
    problem(_client(monkeypatch, world, ADMIN).post(DISABLE, json=body), 422, "validation")
    assert world.params == []
    assert world.events == []


def test_a_secret_shaped_reason_never_comes_back_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scrubbed on write: the stored value, the event and the response all hold the mask."""
    world = World()
    secret = f"rotated token={CANARY} after the incident"
    response = _client(monkeypatch, world, ADMIN).post(DISABLE, json={"reason": secret})
    assert response.status_code == 200
    (sent,) = world.params
    assert CANARY not in sent["reason"]
    assert "[REDACTED]" in sent["reason"]
    assert CANARY not in response.text
    assert all(CANARY not in repr(e) for e in world.events)


def test_a_tenant_scoped_admin_and_a_weaker_role_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Instance scope `admin` only, for both routes."""
    world = World()
    for role in (ACME_ADMIN, OPERATOR):
        client = _client(monkeypatch, world, role)
        problem(client.post(DISABLE, json={"reason": "x"}), 403, "forbidden")
        problem(client.post(ENABLE), 403, "forbidden")
    assert world.params == []


def test_an_unknown_tenant_row_is_a_404_and_writes_no_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tenant vanished between the guard and the write."""
    world = World(_row(tenant_found=False))
    problem(_client(monkeypatch, world, ADMIN).post(ENABLE), 404, "not-found")
    assert world.events == []
    assert world.written == []


def test_a_tenant_deleted_after_the_write_is_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """The final read finds nothing."""
    world = World()
    world.info = None
    problem(_client(monkeypatch, world, ADMIN).post(ENABLE), 404, "not-found")


def test_a_failure_rolls_back_and_leaves_a_failure_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """The error still propagates (a 500); the failure event is its own record."""
    world = World(error=RuntimeError("db down"))
    client = _client(monkeypatch, world, ADMIN, server_errors=False)
    problem(client.post(DISABLE, json={"reason": "x"}), 500, "internal")
    assert world.events == []
    (event,) = world.written
    assert (event.action, event.outcome) == (Action.TENANT_DISABLE, Outcome.FAILURE)
    assert (event.before, event.after) == (None, None)


def test_a_failure_event_that_cannot_be_written_does_not_hide_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best effort only."""
    world = World(error=RuntimeError("db down"))
    client = _client(monkeypatch, world, ADMIN, writer=OSError("audit down"), server_errors=False)
    problem(client.post(ENABLE), 500, "internal")
    assert world.written == []


def test_without_an_audit_writer_the_failure_is_just_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No writer (no database for audit): nothing to record."""
    world = World(error=RuntimeError("db down"))
    client = _client(monkeypatch, world, ADMIN, writer=False, server_errors=False)
    problem(client.post(ENABLE), 500, "internal")


def test_the_mock_mode_without_a_database_is_a_503() -> None:
    """No session factory: 503, as for every database route."""
    env = build(factory=None)
    problem(env.client(ADMIN).post(ENABLE), 503, "unavailable")
