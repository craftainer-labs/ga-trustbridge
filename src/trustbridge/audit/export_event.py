"""The `audit.export` event (ADR 0032 "Events", invariant 13): who exported which range.

Written once per export request by the route, after the stream ended (or was cut off): the
reader's `sub` and `iss`, the scope (a tenant id or `instance`), the filters of the request and
how many rows were sent. `complete` is false when the client went away or the stream failed, in
which case the outcome is `failure` and `last_seq` tells where a resumed export may continue.
"""

from uuid import UUID

from trustbridge.audit.actions import Action
from trustbridge.audit.emit import AuditWriter, _printable
from trustbridge.audit.model import TEXT_LIMITS, ActorType, AuditEvent, AuditQuery, Outcome
from trustbridge.audit.redact import project


async def record_export(  # noqa: PLR0913 - one value per recorded fact
    writer: AuditWriter,
    *,
    sub: str,
    iss: str | None,
    tenant_id: UUID | None,
    query: AuditQuery,
    row_count: int,
    last_seq: int | None,
    complete: bool,
    source_ip: str | None,
    request_id: str | None,
) -> UUID:
    """Write the export's own event in its own transaction (in the tenant's context if any)."""
    details: dict[str, object] = {
        "scope": "instance" if tenant_id is None else "tenant",
        "tenant_id": tenant_id if tenant_id is not None else query.tenant_id,
        "after_seq": query.after_seq,
        "since": query.since,
        "until": query.until,
        "actor_sub": query.actor_sub,
        "action": query.action,
        "resource_type": query.resource_type,
        "resource_id": query.resource_id,
        "outcome": None if query.outcome is None else query.outcome.value,
        "row_count": row_count,
        "last_seq": last_seq,
        "complete": complete,
    }
    return await writer.record(
        AuditEvent(
            actor_type=ActorType.USER,
            actor_sub=_printable(sub, TEXT_LIMITS["actor_sub"]),
            actor_iss=None if iss is None else _printable(iss, TEXT_LIMITS["actor_iss"]),
            action=Action.AUDIT_EXPORT,
            resource_type="audit_events",
            outcome=Outcome.SUCCESS if complete else Outcome.FAILURE,
            tenant_id=tenant_id,
            resource_id="instance" if tenant_id is None else str(tenant_id),
            source_ip=source_ip,
            request_id=request_id,
            after=project("audit_export", {k: v for k, v in details.items() if v is not None}),
        )
    )


__all__ = ["record_export"]
