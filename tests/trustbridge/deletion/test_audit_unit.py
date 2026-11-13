"""The deletion's audit events: ids, states, categories and counts only (ADR 0034 inv. 12, 14)."""

from tests.trustbridge.deletion.fakes import record
from trustbridge.audit import Action, ActorType, Outcome
from trustbridge.deletion import audit


def test_every_event_is_a_system_reconciler_event_of_the_tenant() -> None:
    """Actor, resource and tenant are fixed; the organisation login is nowhere."""
    rec = record(state="github", counts={"secrets": 2})
    events = [
        audit.step_event(rec, "database", rec.counts),
        audit.completed_event(rec, rec.counts),
        audit.failed_event(rec, "bad"),
        *audit.skipped_events(rec, [{"category": "operator_owned", "reason": "keys", "count": 2}]),
    ]
    for event in events:
        assert event.actor_type is ActorType.SYSTEM
        assert event.actor_sub == "reconciler"
        assert event.tenant_id == rec.tenant_id
        assert event.resource_type == "tenant_deletion"
        assert event.resource_id == str(rec.id)
    assert [e.action for e in events] == [
        Action.TENANT_DELETION_STEP,
        Action.TENANT_DELETION_COMPLETED,
        Action.TENANT_DELETION_FAILED,
        Action.TENANT_DELETION_SKIPPED,
    ]
    assert events[2].outcome is Outcome.FAILURE


def test_a_skipped_event_keeps_only_allow_listed_fields() -> None:
    """Repository ids, names and free text in a skipped entry never reach the event."""
    rec = record(state="github")
    (event,) = audit.skipped_events(
        rec,
        [
            {
                "category": "installation_gone",
                "reason": "installation_gone",
                "count": 3,
                "repository_ids": [1, 2, 3],
                "step": "ignored",
                "full_name": "acme/secret-repo",
            }
        ],
    )
    assert event.after == {
        "category": "installation_gone",
        "count": 3,
        "deletion_id": str(rec.id),
        "reason": "installation_gone",
        "step": "github",
    }
