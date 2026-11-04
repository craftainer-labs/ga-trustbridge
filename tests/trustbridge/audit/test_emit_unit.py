"""The emitters without a database: writer context, trace ids, tenant sync and denial events.

ADR 0032 invariants 5 (sub and iss only), 10 (a denial carries the sub, the role or the requested
tenant in `resource_id`, a null `tenant_id`), and "audit writes use the same tenant context as the
action" (0031). The Postgres-backed halves are in `test_emit_integration.py`.
"""

from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.audit import (
    Action,
    ActorType,
    AuditDenials,
    AuditEvent,
    AuditWriter,
    Outcome,
    SystemActor,
    TenantSyncAudit,
    current_trace_id,
    emit,
    with_trace,
)
from trustbridge.rbac import Denial, Role
from trustbridge.tenancy import current_tenant

TRACE = 0x0AF7651916CD43DD8448EB211C80319C


def _event(**overrides: Any) -> AuditEvent:  # noqa: ANN401
    values: dict[str, Any] = {
        "actor_type": ActorType.SYSTEM,
        "actor_sub": "worker",
        "action": Action.TENANT_SYNC,
        "resource_type": "tenant",
        "outcome": Outcome.SUCCESS,
    }
    values.update(overrides)
    return AuditEvent(**values)


def test_no_trace_id_outside_a_span() -> None:
    """Without a valid span there is nothing to correlate."""
    assert current_trace_id() is None
    assert with_trace(_event()).trace_id is None


def test_the_current_span_fills_trace_id_unless_the_event_has_one() -> None:
    """32 lower-case hex characters; an explicit value wins."""
    ctx = SpanContext(TRACE, 1, is_remote=False, trace_flags=TraceFlags(1))
    with trace.use_span(NonRecordingSpan(ctx)):
        assert current_trace_id() == "0af7651916cd43dd8448eb211c80319c"
        assert with_trace(_event()).trace_id == "0af7651916cd43dd8448eb211c80319c"
        assert with_trace(_event(trace_id="mine")).trace_id == "mine"


class FakeSession:
    """Just enough of an `AsyncSession` for `session.begin()` to be an async context."""

    def __init__(self, log: list[str]) -> None:
        """Record opens and commits in `log`."""
        self.log = log

    async def __aenter__(self) -> FakeSession:
        """Record the tenant context the session was opened in."""
        self.log.append(f"open:{current_tenant.get()}")
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Record the close."""
        self.log.append("close")

    def begin(self) -> FakeSession:
        """The same object doubles as the transaction context."""
        return self


async def test_the_writer_inserts_in_its_own_transaction_inside_the_tenant_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The session is opened inside the event's tenant context, then the context is restored."""
    log: list[str] = []
    seen: list[AuditEvent] = []

    async def fake_append(session: object, event: AuditEvent) -> UUID:
        assert isinstance(session, FakeSession)
        seen.append(event)
        return event.id

    monkeypatch.setattr(emit, "append", fake_append)
    factory = cast("async_sessionmaker[AsyncSession]", lambda: FakeSession(log))
    tenant = uuid4()
    event = _event(tenant_id=tenant)
    assert await AuditWriter(factory).record(event) == event.id
    assert log[0] == f"open:{tenant}"
    assert seen == [event]
    assert current_tenant.get() is None
    await AuditWriter(factory).record(_event())  # a null-tenant event opens with no context
    assert log.count("open:None") == 2  # the session and its transaction context


class FakeWriter:
    """Collects the events handed to `record`."""

    def __init__(self) -> None:
        """Start empty."""
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> UUID:
        """Keep the event."""
        self.events.append(event)
        return event.id


async def test_a_tenant_sync_event_names_the_system_actor_the_sender_and_the_delivery() -> None:
    """On behalf of the GitHub sender's numeric id and the delivery id, never a login."""
    writer = FakeWriter()
    tenant = uuid4()
    await TenantSyncAudit(cast("AuditWriter", writer)).tenant_synced(
        tenant_id=tenant, state="suspended", delivery_id="d-1\nforged", sender_id=583231
    )
    (event,) = writer.events
    assert (event.actor_type, event.actor_sub, event.actor_iss) == (
        ActorType.SYSTEM,
        SystemActor.GITHUB_WEBHOOK.value,
        None,
    )
    assert (event.action, event.outcome) == (Action.TENANT_SYNC, Outcome.SUCCESS)
    assert event.tenant_id == tenant
    assert (event.resource_type, event.resource_id) == ("tenant", str(tenant))
    assert event.after == {"state": "suspended"}
    assert event.before is None
    assert event.on_behalf_of == "sender=583231;delivery=d-1forged"


async def test_a_tenant_sync_without_a_sender_has_no_on_behalf_of() -> None:
    """A delivery with no `sender` records none (the delivery id alone is not a person)."""
    writer = FakeWriter()
    await TenantSyncAudit(cast("AuditWriter", writer)).tenant_synced(
        tenant_id=uuid4(), state="active", delivery_id="d-2", sender_id=None
    )
    assert writer.events[0].on_behalf_of is None


def _denial(**overrides: Any) -> Denial:  # noqa: ANN401
    values: dict[str, Any] = {
        "sub": "sub-9",
        "iss": "https://idp.example",
        "reason": "role_missing",
        "required_role": Role.OPERATOR,
        "scope": "instance",
        "tenant_id": None,
        "source_ip": "203.0.113.4",
        "request_id": "req-1",
    }
    values.update(overrides)
    return Denial(**values)


async def test_an_instance_denial_names_the_missing_role_with_a_null_tenant() -> None:
    """Inv. 10: the denied principal's sub and iss, the role in `resource_id`, a null tenant."""
    writer = FakeWriter()
    await AuditDenials(cast("AuditWriter", writer)).denied(_denial())
    (event,) = writer.events
    assert (event.actor_type, event.actor_sub, event.actor_iss) == (
        ActorType.USER,
        "sub-9",
        "https://idp.example",
    )
    assert event.action is Action.AUTHZ_DENIED
    assert event.outcome is Outcome.DENIED
    assert event.tenant_id is None
    assert (event.resource_type, event.resource_id) == ("role", "operator")
    assert event.after == {
        "reason": "role_missing",
        "required_role": "operator",
        "scope": "instance",
    }
    assert (event.source_ip, event.request_id) == ("203.0.113.4", "req-1")


async def test_a_tenant_denial_names_the_requested_tenant_in_resource_id() -> None:
    """Inv. 10: the requested tenant id, `resource_type = tenant`, still a null `tenant_id`."""
    writer = FakeWriter()
    requested = str(uuid4())
    await AuditDenials(cast("AuditWriter", writer)).denied(
        _denial(scope="tenant", reason="tenant_not_granted", tenant_id=requested)
    )
    (event,) = writer.events
    assert (event.resource_type, event.resource_id, event.tenant_id) == ("tenant", requested, None)
    assert event.after is not None
    assert event.after["reason"] == "tenant_not_granted"


async def test_a_tenant_denial_for_a_malformed_id_records_no_resource_id() -> None:
    """An unparsable path value is not stored (it is attacker-chosen text)."""
    writer = FakeWriter()
    await AuditDenials(cast("AuditWriter", writer)).denied(
        _denial(scope="tenant", reason="tenant_not_granted", tenant_id=None)
    )
    assert writer.events[0].resource_id is None
