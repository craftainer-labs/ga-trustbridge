"""The NG6e audit shapes, the shared redaction helpers and the new action names (ADR 0032, 0033)."""

from datetime import UTC, datetime
from typing import Any

import pytest

from trustbridge.audit import Action, project
from trustbridge.audit.redact import (
    REDACTED,
    SECRET_WORDS,
    FailureReason,
    deletion_counts,
    failure_after,
    has_secret_word,
    printable,
)

CANARY = "canary-secret-7f3a9c1e"
WHEN = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def test_the_connection_shape_records_configured_and_nothing_else() -> None:
    """ADR 0032: connection data is `{"configured": true}`; a value, URL or token is dropped."""
    out = project(
        "connection",
        {"configured": True, "base_url": "https://169.254.169.254", "token": CANARY, "pw": CANARY},
    )
    assert out == {"configured": True}
    assert CANARY not in repr(out)


def test_the_tenant_admin_shape_records_the_three_columns_with_the_reason_scrubbed() -> None:
    """Before and after of a disable: time and actor as stored, the free-text reason masked."""
    out = project(
        "tenant_admin",
        {
            "admin_disabled_at": WHEN,
            "admin_disabled_by": "u-1",
            "admin_disable_reason": f"billing hold token={CANARY}",
            "state": "active",
        },
    )
    assert out["admin_disabled_at"] == "2026-11-06T12:00:00Z"
    assert out["admin_disabled_by"] == "u-1"
    assert REDACTED in out["admin_disable_reason"]
    assert CANARY not in repr(out)
    assert "state" not in out
    assert project("tenant_admin", {"admin_disabled_at": None}) == {"admin_disabled_at": None}


def test_the_delivery_shape_never_records_headers_or_the_body() -> None:
    """A requeue event shows the retry state; the error text is scrubbed."""
    out = project(
        "delivery",
        {
            "status": "dead",
            "attempts": 8,
            "next_attempt_at": None,
            "dead_at": WHEN,
            "last_error": f"HTTP 500 authorization: Bearer {CANARY}",
            "headers": {"x-hub-signature-256": CANARY},
            "body": CANARY.encode(),
        },
    )
    assert out["status"] == "dead"
    assert out["attempts"] == 8
    assert CANARY not in repr(out)
    assert set(out) == {"status", "attempts", "next_attempt_at", "dead_at", "last_error"}


def test_the_shared_helpers_drop_control_characters_and_find_secret_words() -> None:
    """One policy for the audit and the admin reads (`admin/views.py` uses both)."""
    assert printable("a\x00b\nc") == "a b c"
    assert printable("plain text") == "plain text"
    assert all(has_secret_word(f"x_{word.upper()}_y") for word in SECRET_WORDS)
    assert has_secret_word("API_KEY")
    assert not has_secret_word("repository_id")


@pytest.mark.parametrize(
    ("member", "value"),
    [
        (Action.TENANT_ENABLE, "tenant.enable"),
        (Action.TENANT_RECONCILE, "tenant.reconcile"),
        (Action.HOST_REGISTRATION_MANUAL, "host.registration.manual"),
        (Action.HOST_REGISTRATION_COMPLETE, "host.registration.complete"),
    ],
)
def test_the_ng6e_action_names_are_the_decided_ones(member: Action, value: str) -> None:
    """User decision 2026-11-06 (they are part of the export contract)."""
    assert member.value == value
    assert Action(value) is member


def test_a_failure_event_records_a_reason_code_and_nothing_else() -> None:
    """NG6e-6: `failure_after` takes only a `FailureReason`; free text cannot get in."""
    assert failure_after(FailureReason.UPSTREAM) == {"reason": "upstream"}
    assert {r.value for r in FailureReason} >= {
        "host_not_ready",
        "secret_unavailable",
        "upstream",
        "lease_lost",
        "conflict",
    }
    free_text: Any = "boom " + CANARY
    with pytest.raises(ValueError, match="FailureReason"):
        failure_after(free_text)
    assert project("failure", {"reason": "upstream", "detail": CANARY}) == {"reason": "upstream"}


def test_failure_reason_for_status_maps_refusals_and_defaults_to_unavailable() -> None:
    """NG6e-6: the one status-to-reason map; only 404 and 409 are refusals."""
    assert FailureReason.for_status(404) is FailureReason.NOT_FOUND
    assert FailureReason.for_status(409) is FailureReason.CONFLICT
    assert FailureReason.for_status(503) is FailureReason.UNAVAILABLE


@pytest.mark.parametrize(
    ("member", "value"),
    [
        (Action.TENANT_DELETION_STEP, "tenant.deletion.step"),
        (Action.TENANT_DELETION_SKIPPED, "tenant.deletion.skipped"),
        (Action.TENANT_DELETION_COMPLETED, "tenant.deletion.completed"),
        (Action.TENANT_DELETION_FAILED, "tenant.deletion.failed"),
    ],
)
def test_the_deletion_action_names_are_the_adr_ones(member: Action, value: str) -> None:
    """ADR 0034 names the events; they are part of the export contract."""
    assert member.value == value
    assert Action(value) is member


def test_the_deletion_shape_records_ids_states_and_codes_and_drops_the_rest() -> None:
    """A row's content, the organisation login or a secret never reach a deletion event."""
    out = project(
        "deletion",
        {
            "deletion_id": "d-1",
            "step": "platform",
            "from_state": "platform",
            "to_state": "github",
            "category": "adapter_resources",
            "reason": "operator_owned",
            "count": 3,
            "attempts": 2,
            "error_code": "bad_config",
            "org": "acme-corp",
            "token": CANARY,
            "last_error": CANARY,
        },
    )
    assert out == {
        "attempts": 2,
        "category": "adapter_resources",
        "count": 3,
        "deletion_id": "d-1",
        "error_code": "bad_config",
        "from_state": "platform",
        "reason": "operator_owned",
        "step": "platform",
        "to_state": "github",
    }


def test_deletion_counts_are_category_to_count_and_nothing_else() -> None:
    """The one non-scalar value: short printable names and non-negative integers."""
    assert deletion_counts({"b": 2, "a": 0}) == {"a": 0, "b": 2}
    assert list(deletion_counts({"b": 2, "a": 0})) == ["a", "b"]
    for bad in ({"": 1}, {"x" * 65: 1}, {"a\nb": 1}, {"a": -1}, {"a": True}, {"a": "1"}, {1: 1}):
        with pytest.raises(ValueError, match="deletion counts"):
            deletion_counts(bad)  # type: ignore[arg-type]
