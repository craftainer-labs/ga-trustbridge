"""The `audit.export` event and the denial summary row (ADR 0032 inv. 13; NG6d decision A6)."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from trustbridge.audit import Action, ActorType, AuditEvent, AuditQuery, AuditWriter, Outcome
from trustbridge.audit.denial import Denial
from trustbridge.audit.emit import AuditDenials
from trustbridge.audit.export_event import record_export


class Writer:
    """Collects events."""

    def __init__(self) -> None:
        """Start empty."""
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> UUID:
        """Keep it."""
        self.events.append(event)
        return event.id


async def test_the_export_event_names_the_reader_the_range_and_the_count() -> None:
    """Inv. 13: who exported, the filters, how many rows."""
    writer = Writer()
    since = datetime(2026, 11, 1, tzinfo=UTC)
    await record_export(
        cast("AuditWriter", writer),
        sub="auditor-1",
        iss="https://idp.example",
        tenant_id=None,
        query=AuditQuery(
            after_seq=10, since=since, action="authz.denied", outcome=Outcome.DENIED, tenant_id=None
        ),
        row_count=42,
        last_seq=99,
        complete=True,
        source_ip="203.0.113.4",
        request_id="req-1",
    )
    (event,) = writer.events
    assert (event.actor_type, event.actor_sub, event.actor_iss) == (
        ActorType.USER,
        "auditor-1",
        "https://idp.example",
    )
    assert (event.action, event.outcome, event.tenant_id) == (
        Action.AUDIT_EXPORT,
        Outcome.SUCCESS,
        None,
    )
    assert (event.resource_type, event.resource_id) == ("audit_events", "instance")
    assert event.after == {
        "scope": "instance",
        "after_seq": 10,
        "since": "2026-11-01T00:00:00Z",
        "action": "authz.denied",
        "outcome": "denied",
        "row_count": 42,
        "last_seq": 99,
        "complete": True,
    }
    assert (event.source_ip, event.request_id) == ("203.0.113.4", "req-1")


async def test_a_tenant_export_event_belongs_to_the_tenant_and_an_aborted_one_is_a_failure() -> (
    None
):
    """The tenant's context carries the row; `complete: false` is outcome `failure`."""
    writer = Writer()
    tenant = uuid4()
    await record_export(
        cast("AuditWriter", writer),
        sub="a",
        iss="https://idp.example",
        tenant_id=tenant,
        query=AuditQuery(),
        row_count=0,
        last_seq=None,
        complete=False,
        source_ip=None,
        request_id=None,
    )
    (event,) = writer.events
    assert (event.tenant_id, event.resource_id, event.outcome) == (
        tenant,
        str(tenant),
        Outcome.FAILURE,
    )
    assert event.after is not None
    assert event.after["scope"] == "tenant"
    assert event.after["tenant_id"] == str(tenant)
    assert event.after["complete"] is False
    assert "last_seq" not in event.after


async def test_a_summary_denial_records_the_suppressed_count_and_names_no_resource() -> None:
    """A6: one row carrying `suppressed` and the window."""
    writer = Writer()
    await AuditDenials(cast("AuditWriter", writer)).denied(
        Denial(
            sub="mallory",
            iss="https://idp.example",
            reason="tenant_not_granted",
            required_role="viewer",
            scope="tenant",
            tenant_id=str(uuid4()),
            source_ip=None,
            request_id=None,
            suppressed=41,
            window_seconds=60,
            window=100,
        )
    )
    (event,) = writer.events
    assert event.resource_id is None
    assert event.after == {
        "reason": "tenant_not_granted",
        "required_role": "viewer",
        "scope": "tenant",
        "suppressed": 41,
        "window_seconds": 60,
        "window": 100,
        "sample": "last",
    }
