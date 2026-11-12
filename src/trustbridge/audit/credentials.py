"""Credential and OIDC-trust events (ADR 0032 "Credential events").

`credential_events` is a pure function from a credential save (what the rows held before, what
they hold now) to the events it implies; `CredentialAuditObserver` appends them in the save's own
transaction, so a credential state change and its event commit together. Metadata only: the
allow-list (`redact`) records the adapter, kind, generation, delivered flag, rotation timestamp
and operation ids, never a value, a platform reference or a connection setting.

| Transition | Event |
| --- | --- |
| a rotate operation is created | `credential.rotate.begin` |
| issue (secret) or a switch to a secret finishes | `credential.issue` |
| rotate finishes | `credential.rotate.finish` |
| delete of a secret, or a switch to a trust, finishes | `credential.revoke` |
| an operation becomes `abandoned` | `credential.operation.abandoned` (outcome `failure`) |
| the credential kind becomes `oidc` | `oidc_trust.create` |
| the credential kind stops being `oidc` | `oidc_trust.remove` |
| the sweep deleted a platform credential nothing accounted for | `credential.orphan.delete` |

An operation ending `failed` (cleanup done, previous state restored) has no event of its own:
ADR 0032 does not list it, and the rollback of a kind change shows as the inverse trust event.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from trustbridge.adapters.store import (
    CredentialKind,
    Operation,
    OperationKind,
    OperationStatus,
    StoredCredential,
)
from trustbridge.audit.actions import Action, SystemActor
from trustbridge.audit.emit import AuditWriter, with_trace
from trustbridge.audit.model import ActorType, AuditEvent, Outcome
from trustbridge.audit.redact import project
from trustbridge.audit.store import append

_MAX_RESOURCE_ID = 255


def _bounded_id(value: str) -> str:
    """`value` as an event `resource_id`: printable characters only, within the column limit."""
    return "".join(c for c in value if c.isprintable())[:_MAX_RESOURCE_ID]


@dataclass(frozen=True, slots=True)
class CredentialEvent:
    """One event implied by a save, before it gets an actor."""

    action: Action
    outcome: Outcome
    resource_type: str
    before: dict[str, object] | None
    after: dict[str, object] | None


def _view(stored: StoredCredential, operation: Operation | None) -> dict[str, object]:
    """The allow-listed facts about a credential and its operation, as a source for `project`."""
    source: dict[str, object] = {"adapter": stored.adapter, "kind": stored.kind}
    current, previous = stored.state.current, stored.state.previous
    if current is not None:
        source["generation"] = current.generation
        source["delivered"] = current.delivered
    if previous is not None:
        source["previous_generation"] = previous.generation
        source["retire_after"] = previous.retire_after
    if operation is not None:
        source["operation_id"] = operation.id
        source["operation_kind"] = operation.kind.value
    return source


def _base_view(stored: StoredCredential, operation: Operation) -> dict[str, object] | None:
    """What the credential was before the operation, from its write-ahead `base_state`."""
    base = operation.base_state
    if base is None:
        return None
    return _view(
        StoredCredential(
            tenant_id=stored.tenant_id,
            adapter=stored.adapter,
            scope_key=stored.scope_key,
            scope=stored.scope,
            kind=operation.base_kind,
            state=base,
        ),
        None,
    )


def _operation_action(operation: Operation) -> Action | None:
    """The credential event a *finished* operation stands for, if any."""
    secret_target = not operation.oidc_offered
    match operation.kind:
        case OperationKind.ISSUE if secret_target:
            return Action.CREDENTIAL_ISSUE
        case OperationKind.SWITCH:
            return Action.CREDENTIAL_ISSUE if secret_target else Action.CREDENTIAL_REVOKE
        case OperationKind.ROTATE:
            return Action.CREDENTIAL_ROTATE_FINISH
        case OperationKind.DELETE if operation.base_kind == "secret":
            return Action.CREDENTIAL_REVOKE
        case _:
            return None


def credential_events(
    *,
    previous_kind: CredentialKind | None,
    previous_status: OperationStatus | None,
    credential: StoredCredential,
    operation: Operation | None,
) -> list[CredentialEvent]:
    """The events a save implies, in order. Re-saving an unchanged status or kind yields none."""
    events: list[CredentialEvent] = []
    if operation is not None and operation.status != previous_status:
        base = _base_view(credential, operation)
        before = None if base is None else project("credential", base)
        after = project("credential", _view(credential, operation))
        action: Action | None = None
        outcome = Outcome.SUCCESS
        if previous_status is None and operation.kind is OperationKind.ROTATE:
            events.append(
                CredentialEvent(
                    Action.CREDENTIAL_ROTATE_BEGIN, Outcome.SUCCESS, "credential", before, after
                )
            )
        # An operation first saved already terminal still gets its closing event.
        if operation.status is OperationStatus.FINISHED:
            action = _operation_action(operation)
        elif operation.status is OperationStatus.ABANDONED:
            action, outcome = Action.CREDENTIAL_OPERATION_ABANDONED, Outcome.FAILURE
        if action is not None:
            events.append(CredentialEvent(action, outcome, "credential", before, after))
    if previous_kind != credential.kind:
        source: dict[str, object] = {"adapter": credential.adapter}
        if operation is not None:
            source["operation_id"] = operation.id
        trust = project("oidc_trust", source)
        if credential.kind == "oidc":
            events.append(
                CredentialEvent(
                    Action.OIDC_TRUST_CREATE, Outcome.SUCCESS, "oidc_trust", None, trust
                )
            )
        elif previous_kind == "oidc":
            events.append(
                CredentialEvent(
                    Action.OIDC_TRUST_REMOVE, Outcome.SUCCESS, "oidc_trust", trust, None
                )
            )
    return events


class CredentialAuditObserver:
    """The audit log's `SaveObserver` and `OrphanObserver` for the credential core.

    `actor` is the process running the core (`worker`, `reconciler` or `api`).
    """

    def __init__(self, writer: AuditWriter, actor: SystemActor) -> None:
        """`writer` serves the sweep's events, which have no save to join."""
        self._writer = writer
        self._actor = actor

    def _event(self, tenant_id: UUID, resource_id: str, implied: CredentialEvent) -> AuditEvent:
        return AuditEvent(
            actor_type=ActorType.SYSTEM,
            actor_sub=self._actor.value,
            action=implied.action,
            resource_type=implied.resource_type,
            outcome=implied.outcome,
            tenant_id=tenant_id,
            resource_id=resource_id,
            before=implied.before,
            after=implied.after,
        )

    async def saved(
        self,
        session: AsyncSession,
        *,
        previous_kind: CredentialKind | None,
        previous_status: OperationStatus | None,
        credential: StoredCredential,
        operation: Operation | None,
    ) -> None:
        """Append the implied events in the save's transaction (the session passed in)."""
        for implied in credential_events(
            previous_kind=previous_kind,
            previous_status=previous_status,
            credential=credential,
            operation=operation,
        ):
            event = self._event(credential.tenant_id, _bounded_id(credential.scope_key), implied)
            await append(session, with_trace(event))

    async def orphan_deleted(self, tenant_id: UUID, adapter: str, name: str) -> None:
        """Write `credential.orphan.delete`; `name` is the platform credential name."""
        printable = _bounded_id(name)
        implied = CredentialEvent(
            Action.CREDENTIAL_ORPHAN_DELETE,
            Outcome.SUCCESS,
            "credential",
            None,
            project("credential", {"adapter": adapter}),
        )
        await self._writer.record(self._event(tenant_id, printable, implied))
