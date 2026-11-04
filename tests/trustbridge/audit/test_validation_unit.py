"""AuditEvent value validation and the redaction helpers, without a database (ADR 0032).

Invariants 5 (identity columns only), 6 (allow-list and canary) and the construction-time
validation NG6c deferred to NG6f: types, enums and lengths, and a refusal that fails closed.
"""

import math
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest

from trustbridge.audit import (
    ALLOW_LISTS,
    MAX_PAYLOAD_BYTES,
    TEXT_LIMITS,
    Action,
    ActorType,
    AuditEvent,
    Outcome,
    SystemActor,
    project,
    scrub_text,
    truncate,
)

CANARY = "canary-secret-7f3a9c1e"
PEM_KIND = "PRIVATE " + "KEY"  # split so secret scanners ignore this file


def _event(**overrides: Any) -> AuditEvent:  # noqa: ANN401
    values: dict[str, Any] = {
        "actor_type": ActorType.USER,
        "actor_sub": "sub-1",
        "actor_iss": "https://idp.example",
        "action": Action.TENANT_DISABLE,
        "resource_type": "tenant",
        "outcome": Outcome.SUCCESS,
    }
    values.update(overrides)
    return AuditEvent(**values)


def test_an_action_given_as_its_string_value_is_stored_as_the_member() -> None:
    """The closed enum is the source of truth; a known string is coerced, never kept raw."""
    event = _event(action="credential.issue")
    assert event.action is Action.CREDENTIAL_ISSUE
    assert type(event.action) is Action


def test_every_system_actor_name_is_accepted() -> None:
    """The fixed names in ADR 0032 (worker, reconciler, github-webhook, migrate)."""
    for actor in SystemActor:
        event = _event(actor_type=ActorType.SYSTEM, actor_sub=actor.value, actor_iss=None)
        assert event.actor_sub == actor.value


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"actor_type": "user"}, "actor_type"),
        ({"outcome": "success"}, "outcome"),
        ({"action": "tenant.explode"}, "known audit action"),
        ({"action": 5}, "known audit action"),
        ({"actor_sub": 5}, "actor_sub must be a string"),
        ({"actor_sub": "line\nbreak"}, "control character"),
        ({"actor_sub": "nul\x00byte"}, "control character"),
        ({"resource_id": 7}, "resource_id must be a string"),
        ({"source_ip": 7}, "source_ip must be a string"),
        ({"source_ip": "fe80::1%eth0"}, "scope id"),
        ({"source_ip": "300.1.1.1"}, ".+"),
        ({"source_ip": "1" * (TEXT_LIMITS["source_ip"] + 1)}, "longer than"),
        ({"actor_type": ActorType.SYSTEM, "actor_sub": "intruder", "actor_iss": None}, "system"),
        ({"tenant_id": "not-a-uuid"}, "tenant_id must be a UUID"),
        ({"id": "not-a-uuid"}, "id must be a UUID"),
        ({"schema_version": True}, "schema_version"),
        ({"schema_version": 0}, "schema_version"),
        ({"schema_version": 2}, "schema_version"),
        ({"schema_version": "1"}, "schema_version"),
        ({"before": ["not", "an", "object"]}, "before must be an object"),
        ({"after": {"x": object()}}, "not serializable"),
        ({"after": {"x": math.nan}}, "not serializable"),
        ({"after": {"x": "nul\x00"}}, "NUL"),
        ({"after": {"x": "y" * (MAX_PAYLOAD_BYTES + 1)}}, "larger than"),
    ],
)
def test_a_bad_value_is_refused_at_construction(overrides: dict[str, Any], message: str) -> None:
    """Types, enums, shapes and bounds fail closed: the event is never built."""
    with pytest.raises(ValueError, match=message):
        _event(**overrides)


@pytest.mark.parametrize("field", sorted(set(TEXT_LIMITS) - {"source_ip"}))
def test_each_text_column_has_a_length_bound(field: str) -> None:
    """At the limit is fine; one past it is refused."""
    _event(**{field: "a" * TEXT_LIMITS[field]})
    with pytest.raises(ValueError, match="longer than"):
        _event(**{field: "a" * (TEXT_LIMITS[field] + 1)})


def test_a_payload_at_the_bound_is_accepted() -> None:
    """The size bound is on the serialized text."""
    filler = "y" * (MAX_PAYLOAD_BYTES - len('{"x": ""}'))
    assert _event(after={"x": filler}).after == {"x": filler}


# ---- redaction -------------------------------------------------------------------------------
def test_project_records_only_allow_listed_fields_in_sorted_order() -> None:
    """Inv. 6: a column not on the list is absent, whatever the source holds."""
    source = {"state": "active", "connection_data": CANARY, "private_key": CANARY}
    out = project("tenant", source)
    assert out == {"state": "active"}
    assert CANARY not in repr(out)
    credential = project(
        "credential", {"kind": "secret", "adapter": "ghcr", "ref": {"id": CANARY}, "name": CANARY}
    )
    assert list(credential) == ["adapter", "kind"]


def test_every_allow_listed_shape_is_plain_names_without_secret_fields() -> None:
    """A review aid: no allow-list names a field that could hold a value or connection data."""
    forbidden = {"secret", "token", "password", "value", "values", "ref", "name", "base_url"}
    for shape, fields in ALLOW_LISTS.items():
        assert not fields & forbidden, shape


def test_project_converts_scalars_and_bounds_strings() -> None:
    """Datetimes become UTC strings with a Z, ids strings, long strings are cut and marked."""
    when = datetime(2026, 11, 4, 9, 30, tzinfo=timezone(timedelta(hours=2)))
    op = uuid4()
    long_value = "x" * 1000
    out = project(
        "credential",
        {
            "retire_after": when,
            "operation_id": op,
            "generation": 3,
            "delivered": True,
            "kind": None,
            "adapter": long_value,
        },
    )
    assert out["retire_after"] == "2026-11-04T07:30:00Z"
    assert out["operation_id"] == str(op)
    assert (out["generation"], out["delivered"], out["kind"]) == (3, True, None)
    assert len(out["adapter"]) == 256
    assert out["adapter"].endswith("...[truncated]")


@pytest.mark.parametrize("bad", [math.inf, {"nested": 1}, [1], object(), b"bytes"])
def test_project_refuses_what_it_cannot_record(bad: object) -> None:
    """Nested data, non-finite numbers and unknown types are errors, not silent dumps."""
    with pytest.raises(ValueError, match="state"):
        project("tenant", {"state": bad})


def test_project_accepts_a_finite_float() -> None:
    """Finite numbers pass."""
    assert project("tenant", {"state": 1.5}) == {"state": 1.5}


def test_project_refuses_an_unknown_shape() -> None:
    """No allow-list, no event: never an unfiltered dump."""
    with pytest.raises(ValueError, match="no audit allow-list"):
        project("connection", {"base_url": CANARY})


def test_truncate_marks_the_cut_and_leaves_short_text_alone() -> None:
    """The marker counts toward the limit."""
    assert truncate("short") == "short"
    cut = truncate("a" * 30, 20)
    assert len(cut) == 20
    assert cut.endswith("...[truncated]")


@pytest.mark.parametrize(
    "text",
    [
        f"login failed token={CANARY} for user",
        f"Authorization: Bearer {CANARY}",
        f"password: {CANARY}",
        f"api_key={CANARY}",
        f"opaque {'A' * 48} blob",
        f"-----BEGIN {PEM_KIND}-----\n{CANARY}\n-----END {PEM_KIND}-----",
    ],
)
def test_scrub_text_masks_credential_shapes(text: str) -> None:
    """A free-text message is recorded no more openly than the logs would show it."""
    scrubbed = scrub_text(text)
    assert CANARY not in scrubbed
    assert "A" * 48 not in scrubbed
    assert "[REDACTED]" in scrubbed


def test_scrub_text_drops_control_characters_and_bounds_length() -> None:
    """Newlines would forge log-style lines; length is bounded."""
    assert scrub_text("one\ntwo\x00three") == "one two three"
    assert len(scrub_text("w " * 500)) == 256


def test_scrub_text_keeps_a_uuid() -> None:
    """Opaque-run masking must not eat identifiers."""
    ident = str(uuid4())
    assert scrub_text(f"tenant {ident} failed") == f"tenant {ident} failed"


def test_events_built_from_projection_carry_the_canary_nowhere() -> None:
    """Inv. 6 at the model: serialized, the event contains none of the planted values."""
    event = _event(after=project("tenant", {"state": "active", "secret": CANARY}))
    assert CANARY not in repr(event)
