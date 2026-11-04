"""Credential and OIDC-trust events derived from a save (ADR 0032 "Credential events").

Pure derivation plus the observer with `append` and the writer faked; the same-transaction
behaviour against Postgres is in `test_emit_integration.py`.
"""

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import trustbridge.audit.credentials as creds
from trustbridge.adapters.interface import CredentialSlot, CredentialState
from trustbridge.adapters.store import (
    CredentialKind,
    Operation,
    OperationKind,
    OperationStatus,
    ScopeId,
    StoredCredential,
)
from trustbridge.audit import Action, ActorType, AuditEvent, AuditWriter, Outcome, SystemActor
from trustbridge.audit.credentials import CredentialAuditObserver, credential_events

CANARY = "canary-secret-7f3a9c1e"
TENANT = uuid4()
SCOPE = ScopeId(repository_id=10, repository_owner_id=5, environment="prod", permission="push")
RETIRE = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def _slot(generation: int, **overrides: Any) -> CredentialSlot:  # noqa: ANN401
    values: dict[str, Any] = {
        "generation": generation,
        "name": f"tb-{CANARY}-g{generation}",
        "ref": {"robot_id": CANARY},
        "delivered": True,
    }
    values.update(overrides)
    return CredentialSlot(**values)


def _stored(kind: CredentialKind | None, state: CredentialState) -> StoredCredential:
    return StoredCredential(
        tenant_id=TENANT, adapter="ghcr", scope_key="k1", scope=SCOPE, kind=kind, state=state
    )


def _op(
    kind: OperationKind,
    status: OperationStatus,
    *,
    offered: bool = False,
    base_kind: CredentialKind | None = None,
    base: CredentialState | None = None,
) -> Operation:
    return Operation(
        id=uuid4(),
        tenant_id=TENANT,
        adapter="ghcr",
        scope_key="k1",
        kind=kind,
        status=status,
        oidc_offered=offered,
        base_state=base,
        base_kind=base_kind,
    )


SECRET_NOW = _stored(
    "secret", CredentialState(current=_slot(2), previous=_slot(1, retire_after=RETIRE))
)
OLD = CredentialState(current=_slot(1))


def _actions(**kwargs: Any) -> list[Action]:  # noqa: ANN401
    return [e.action for e in credential_events(**kwargs)]


@pytest.mark.parametrize(
    ("kind", "offered", "base_kind", "expected"),
    [
        (OperationKind.ISSUE, False, None, [Action.CREDENTIAL_ISSUE]),
        (OperationKind.ISSUE, True, None, []),
        (OperationKind.SWITCH, False, "oidc", [Action.CREDENTIAL_ISSUE]),
        (OperationKind.SWITCH, True, "secret", [Action.CREDENTIAL_REVOKE]),
        (OperationKind.ROTATE, False, "secret", [Action.CREDENTIAL_ROTATE_FINISH]),
        (OperationKind.DELETE, False, "secret", [Action.CREDENTIAL_REVOKE]),
        (OperationKind.DELETE, False, "oidc", []),
    ],
)
def test_a_finished_operation_stands_for_one_credential_event(
    kind: OperationKind, offered: bool, base_kind: CredentialKind | None, expected: list[Action]
) -> None:
    """Issue, rotate finish and revoke are recorded when the operation reaches `finished`."""
    op = _op(kind, OperationStatus.FINISHED, offered=offered, base_kind=base_kind)
    got = _actions(
        previous_kind=SECRET_NOW.kind,
        previous_status=OperationStatus.DELIVERED,
        credential=SECRET_NOW,
        operation=op,
    )
    assert got == expected


def test_a_new_rotate_operation_is_a_begin_event_with_before_and_after() -> None:
    """Rotate begin names the operation and the generations; `before` is the base state."""
    op = _op(OperationKind.ROTATE, OperationStatus.INTENT, base_kind="secret", base=OLD)
    (event,) = credential_events(
        previous_kind="secret", previous_status=None, credential=SECRET_NOW, operation=op
    )
    assert event.action is Action.CREDENTIAL_ROTATE_BEGIN
    assert event.outcome is Outcome.SUCCESS
    assert event.resource_type == "credential"
    assert event.before == {"adapter": "ghcr", "delivered": True, "generation": 1, "kind": "secret"}
    assert event.after == {
        "adapter": "ghcr",
        "delivered": True,
        "generation": 2,
        "kind": "secret",
        "operation_id": str(op.id),
        "operation_kind": "rotate",
        "previous_generation": 1,
        "retire_after": "2026-11-05T12:00:00Z",
    }


def test_a_begin_without_a_base_state_has_no_before() -> None:
    """The first operation on a scope has nothing before it."""
    op = _op(OperationKind.ROTATE, OperationStatus.INTENT)
    (event,) = credential_events(
        previous_kind=None, previous_status=None, credential=SECRET_NOW, operation=op
    )
    assert event.before is None


def test_an_abandoned_operation_is_a_failure_event() -> None:
    """ADR 0032 lists the abandoned operation; its outcome is `failure`."""
    op = _op(OperationKind.ROTATE, OperationStatus.ABANDONED)
    (event,) = credential_events(
        previous_kind="secret",
        previous_status=OperationStatus.CLEANUP,
        credential=SECRET_NOW,
        operation=op,
    )
    assert (event.action, event.outcome) == (
        Action.CREDENTIAL_OPERATION_ABANDONED,
        Outcome.FAILURE,
    )


@pytest.mark.parametrize(
    ("kind", "status", "previous"),
    [
        (OperationKind.ISSUE, OperationStatus.INTENT, None),
        (OperationKind.ROTATE, OperationStatus.PARKED, OperationStatus.INTENT),
        (OperationKind.ROTATE, OperationStatus.FAILED, OperationStatus.CLEANUP),
        (OperationKind.ROTATE, OperationStatus.DELIVERED, OperationStatus.DELIVERED),
        (OperationKind.ROTATE, OperationStatus.FINISHED, OperationStatus.FINISHED),
    ],
)
def test_intermediate_failed_and_repeated_statuses_record_nothing(
    kind: OperationKind, status: OperationStatus, previous: OperationStatus | None
) -> None:
    """Only the listed transitions are events; a re-save of the same status is silent."""
    assert not _actions(
        previous_kind="secret",
        previous_status=previous,
        credential=SECRET_NOW,
        operation=_op(kind, status),
    )


def test_a_save_without_an_operation_and_without_a_kind_change_records_nothing() -> None:
    """A refresh of an unchanged OIDC scope is not an event."""
    oidc = _stored("oidc", CredentialState(current=_slot(1)))
    assert not credential_events(
        previous_kind="oidc", previous_status=None, credential=oidc, operation=None
    )


def test_an_oidc_trust_created_and_removed_follow_the_kind() -> None:
    """The kind becoming `oidc` creates a trust; leaving `oidc` removes it."""
    oidc = _stored("oidc", CredentialState(current=_slot(1)))
    (created,) = credential_events(
        previous_kind=None, previous_status=None, credential=oidc, operation=None
    )
    assert created.action is Action.OIDC_TRUST_CREATE
    assert (created.resource_type, created.before, created.after) == (
        "oidc_trust",
        None,
        {"adapter": "ghcr"},
    )
    op = _op(OperationKind.DELETE, OperationStatus.FINISHED, base_kind="oidc")
    gone = _stored(None, CredentialState(current=None))
    (removed,) = credential_events(
        previous_kind="oidc",
        previous_status=OperationStatus.DELIVERED,
        credential=gone,
        operation=op,
    )
    assert removed.action is Action.OIDC_TRUST_REMOVE
    assert removed.before == {"adapter": "ghcr", "operation_id": str(op.id)}
    assert removed.after is None


def test_a_switch_to_a_trust_revokes_the_secret_and_creates_the_trust() -> None:
    """Both events come from one save, revoke first."""
    op = _op(OperationKind.SWITCH, OperationStatus.FINISHED, offered=True, base_kind="secret")
    oidc = _stored("oidc", CredentialState(current=_slot(2)))
    got = _actions(
        previous_kind="secret",
        previous_status=OperationStatus.DELIVERED,
        credential=oidc,
        operation=op,
    )
    assert got == [Action.CREDENTIAL_REVOKE, Action.OIDC_TRUST_CREATE]


def test_no_event_carries_a_name_a_reference_or_a_value() -> None:
    """Inv. 6: the canary in a slot's name and platform reference reaches no event."""
    op = _op(OperationKind.ROTATE, OperationStatus.INTENT, base_kind="secret", base=OLD)
    events = credential_events(
        previous_kind="secret", previous_status=None, credential=SECRET_NOW, operation=op
    )
    assert events
    assert CANARY not in repr(events)


class RecordingSession:
    """Stands in for the save's session; the faked `append` collects what it was given."""


async def test_the_observer_appends_each_event_through_the_saves_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same transaction: `append` receives the session the save used, not a new one."""
    appended: list[tuple[object, AuditEvent]] = []

    async def fake_append(session: object, event: AuditEvent) -> UUID:
        appended.append((session, event))
        return event.id

    monkeypatch.setattr(creds, "append", fake_append)
    session = RecordingSession()
    op = _op(OperationKind.SWITCH, OperationStatus.FINISHED, offered=True, base_kind="secret")
    oidc = _stored("oidc", CredentialState(current=_slot(2)))
    observer = CredentialAuditObserver(cast("AuditWriter", object()), SystemActor.RECONCILER)
    await observer.saved(
        cast("AsyncSession", session),
        previous_kind="secret",
        previous_status=OperationStatus.DELIVERED,
        credential=oidc,
        operation=op,
    )
    assert [e.action for _, e in appended] == [Action.CREDENTIAL_REVOKE, Action.OIDC_TRUST_CREATE]
    assert all(s is session for s, _ in appended)
    event = appended[0][1]
    assert (event.actor_type, event.actor_sub, event.actor_iss) == (
        ActorType.SYSTEM,
        "reconciler",
        None,
    )
    assert (event.tenant_id, event.resource_id) == (TENANT, "k1")


class FakeWriter:
    """Collects the events handed to `record`."""

    def __init__(self) -> None:
        """Start empty."""
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> UUID:
        """Keep the event."""
        self.events.append(event)
        return event.id


async def test_an_orphan_deletion_is_recorded_with_the_platform_name_only() -> None:
    """The sweep has no save to join: one event of its own, naming the credential, no value."""
    writer = FakeWriter()
    observer = CredentialAuditObserver(cast("AuditWriter", writer), SystemActor.WORKER)
    await observer.orphan_deleted(TENANT, "ghcr", "tb-abc-k1-g3\nforged")
    (event,) = writer.events
    assert event.action is Action.CREDENTIAL_ORPHAN_DELETE
    assert (event.tenant_id, event.resource_type, event.resource_id) == (
        TENANT,
        "credential",
        "tb-abc-k1-g3forged",
    )
    assert event.after == {"adapter": "ghcr"}
    assert event.actor_sub == "worker"
