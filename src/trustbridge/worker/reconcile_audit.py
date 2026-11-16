"""The summary event of one processed tenant reconcile request (NG5r-c, ADR 0032 and 0033).

The credential events of a reconcile are written by the store's observer as the body changes
things (actor `reconciler`). This module adds the one *terminal* event of the request itself:
`tenant.reconcile.completed` when the body ran to the end, `tenant.reconcile.failed` when the
request was dropped (the tenant is disabled, deleting or gone; the body was stopped because the
tenant was disabled; or it failed `TENANT_RECONCILE_MAX_ATTEMPTS` times). A run that failed and
will be retried writes nothing, so a request has one terminal event. The reconciler writes it
BEFORE it acknowledges the request (see `Reconciler._reconcile_claim`), keyed by the request's id
(`ReconcileQueue.request_id`, the same for every run of one request): a crash after the write and
before the ack runs the request again, and the second write is absorbed by the database
(`audit.store.append`, `event_key`). The event is written at least once and stored once; it is
never lost to a crash between the ack and the write.

Residuals, accepted: the key is `(tenant_id, action, event_key)`, so a request whose first run
wrote `completed`, crashed before the ack, and settled as `failed` on the rerun (the tenant was
disabled meanwhile) has both events, each true; and the request id lives in Valkey until the ack
removes it, so a hand edit of the queue keys can leave one behind (the next request of that
tenant would then reuse it and its event be absorbed).

The payload is a fixed reason code (`FailureReason`) and counts; never an error text, a URL or a
value. The event is written in the request's tenant context, also for a tenant that no longer
exists (the insert policy admits the context tenant, and `audit_events` has no foreign key).
"""

from dataclasses import dataclass
from uuid import UUID

from trustbridge.audit.actions import Action, SystemActor
from trustbridge.audit.emit import AuditWriter
from trustbridge.audit.model import ActorType, AuditEvent, Outcome
from trustbridge.audit.redact import FailureReason, project

RESOURCE_TYPE = "tenant"


@dataclass(frozen=True, slots=True)
class ReconcileSummary:
    """What one run of the body did (counts), and why it stopped early if it did."""

    operations_resumed: int = 0
    scopes_ensured: int = 0
    orphans_swept: int = 0
    parked_values_swept: int = 0
    stopped: FailureReason | None = None


def reconcile_event(
    tenant_id: UUID,
    *,
    event_key: str | None = None,
    reason: FailureReason | None = None,
    summary: ReconcileSummary | None = None,
    attempts: int | None = None,
) -> AuditEvent:
    """`tenant.reconcile.completed` without a `reason`, else `tenant.reconcile.failed`."""
    after: dict[str, object] = {}
    if reason is not None:
        after["reason"] = reason.value
    if attempts is not None:
        after["attempts"] = attempts
    if summary is not None:
        after |= {
            "operations_resumed": summary.operations_resumed,
            "scopes_ensured": summary.scopes_ensured,
            "orphans_swept": summary.orphans_swept,
            "parked_values_swept": summary.parked_values_swept,
        }
    failed = reason is not None
    return AuditEvent(
        actor_type=ActorType.SYSTEM,
        actor_sub=SystemActor.RECONCILER,
        action=Action.TENANT_RECONCILE_FAILED if failed else Action.TENANT_RECONCILE_COMPLETED,
        resource_type=RESOURCE_TYPE,
        resource_id=str(tenant_id),
        outcome=Outcome.FAILURE if failed else Outcome.SUCCESS,
        tenant_id=tenant_id,
        event_key=event_key,
        after=project("tenant_reconcile", after),
    )


class ReconcileAudit:
    """Writes the terminal event of a reconcile request in its own transaction."""

    def __init__(self, writer: AuditWriter) -> None:
        """Write through `writer` (the event's tenant is the context)."""
        self._writer = writer

    async def settled(
        self,
        tenant_id: UUID,
        request_id: str,
        *,
        reason: FailureReason | None = None,
        summary: ReconcileSummary | None = None,
        attempts: int | None = None,
    ) -> None:
        """Write one terminal event; `reason` makes it the `failed` one.

        `request_id` is the event's key: writing the same request's event again stores nothing.
        """
        await self._writer.record(
            reconcile_event(
                tenant_id, event_key=request_id, reason=reason, summary=summary, attempts=attempts
            )
        )
