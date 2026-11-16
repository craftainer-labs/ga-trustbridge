"""The terminal event of a tenant reconcile request (NG5r-c): its action, shape and payload."""

from typing import Any
from uuid import uuid4

import pytest

from trustbridge.audit import Action, ActorType, AuditEvent, Outcome
from trustbridge.audit.redact import FailureReason
from trustbridge.worker.reconcile_audit import ReconcileAudit, ReconcileSummary, reconcile_event

COUNTS = ReconcileSummary(
    operations_resumed=1, scopes_ensured=2, orphans_swept=3, parked_values_swept=4
)


def test_a_completed_event_carries_the_counts_and_no_reason() -> None:
    """Success outcome, actor `reconciler`, the request's tenant."""
    tenant = uuid4()
    event = reconcile_event(tenant, summary=COUNTS)
    assert event.action is Action.TENANT_RECONCILE_COMPLETED
    assert event.outcome is Outcome.SUCCESS
    assert (event.actor_type, event.actor_sub, event.actor_iss) == (
        ActorType.SYSTEM,
        "reconciler",
        None,
    )
    assert (event.tenant_id, event.resource_type, event.resource_id) == (
        tenant,
        "tenant",
        str(tenant),
    )
    assert event.after == {
        "operations_resumed": 1,
        "scopes_ensured": 2,
        "orphans_swept": 3,
        "parked_values_swept": 4,
    }
    assert event.before is None


@pytest.mark.parametrize(
    "reason",
    [
        FailureReason.TENANT_DISABLED,
        FailureReason.TENANT_DELETING,
        FailureReason.TENANT_GONE,
        FailureReason.ATTEMPTS_EXCEEDED,
    ],
)
def test_a_failed_event_carries_a_fixed_reason_code(reason: FailureReason) -> None:
    """Failure outcome; a drop has the reason and nothing else."""
    event = reconcile_event(uuid4(), reason=reason)
    assert event.action is Action.TENANT_RECONCILE_FAILED
    assert event.outcome is Outcome.FAILURE
    assert event.after is not None
    assert event.after["reason"] == reason.value
    assert set(event.after) <= {"reason", "attempts"}


def test_a_stopped_run_keeps_its_partial_counts() -> None:
    """A tenant disabled mid-run: the reason and what ran before the stop."""
    event = reconcile_event(uuid4(), reason=FailureReason.TENANT_DISABLED, summary=COUNTS)
    assert event.after is not None
    assert event.after["reason"] == "tenant_disabled"
    assert event.after["scopes_ensured"] == 2


async def test_the_writer_records_one_event_per_call() -> None:
    """`settled` hands exactly the built event to the audit writer."""
    written: list[AuditEvent] = []

    class Writer:
        async def record(self, event: AuditEvent) -> None:
            written.append(event)

    tenant = uuid4()
    audit: Any = ReconcileAudit(Writer())  # type: ignore[arg-type]
    await audit.settled(tenant, summary=COUNTS)
    [event] = written
    assert event.action is Action.TENANT_RECONCILE_COMPLETED
    assert event.tenant_id == tenant


def test_an_exhausted_request_records_its_attempt_count() -> None:
    """`attempts_exceeded` names how many runs failed."""
    event = reconcile_event(uuid4(), reason=FailureReason.ATTEMPTS_EXCEEDED, attempts=5)
    assert event.after == {"attempts": 5, "reason": "attempts_exceeded"}
