"""The deletion's audit events (ADR 0034 "The audit trail"; NG6h-4a ruling 2).

Ids, state and step names, categories, counts and a fixed code only: no organisation login and
no row content. The store writes them in the transaction of the transition they record.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from trustbridge.audit import Action, ActorType, AuditEvent, Outcome, SystemActor, project
from trustbridge.audit.redact import deletion_counts
from trustbridge.deletion.model import DeletionRecord

RESOURCE_TYPE = "tenant_deletion"


def _event(
    record: DeletionRecord,
    action: Action,
    outcome: Outcome,
    after: Mapping[str, Any],
    counts: Mapping[str, int] | None = None,
) -> AuditEvent:
    payload = project("deletion", {"deletion_id": str(record.id), **after})
    if counts is not None:
        payload["counts"] = deletion_counts(counts)
    return AuditEvent(
        actor_type=ActorType.SYSTEM,
        actor_sub=SystemActor.RECONCILER,
        action=action,
        resource_type=RESOURCE_TYPE,
        resource_id=str(record.id),
        outcome=outcome,
        tenant_id=record.tenant_id,
        before=project("deletion", {"deletion_id": str(record.id), "from_state": record.state}),
        after=payload,
    )


def step_event(record: DeletionRecord, to_state: str, counts: Mapping[str, int]) -> AuditEvent:
    """`tenant.deletion.step`: the step `record.state` completed; `counts` are cumulative."""
    return _event(
        record,
        Action.TENANT_DELETION_STEP,
        Outcome.SUCCESS,
        {"step": record.state, "from_state": record.state, "to_state": to_state},
        counts,
    )


def skipped_events(
    record: DeletionRecord, skipped: Sequence[Mapping[str, Any]]
) -> list[AuditEvent]:
    """One `tenant.deletion.skipped` per skipped category (`category`, `reason`, `count`)."""
    return [
        _event(
            record,
            Action.TENANT_DELETION_SKIPPED,
            Outcome.SUCCESS,
            {"step": record.state, **{k: v for k, v in item.items() if k != "step"}},
        )
        for item in skipped
    ]


def completed_event(record: DeletionRecord, counts: Mapping[str, int]) -> AuditEvent:
    """`tenant.deletion.completed`, written with the tenant row already removed."""
    return _event(
        record,
        Action.TENANT_DELETION_COMPLETED,
        Outcome.SUCCESS,
        {"step": record.state, "to_state": "done"},
        counts,
    )


def failed_event(record: DeletionRecord, code: str) -> AuditEvent:
    """`tenant.deletion.failed`: a non-retryable error ended the deletion (the code only)."""
    return _event(
        record,
        Action.TENANT_DELETION_FAILED,
        Outcome.FAILURE,
        {
            "step": record.state,
            "to_state": "failed",
            "error_code": code,
            "attempts": record.attempts,
        },
        record.counts,
    )
