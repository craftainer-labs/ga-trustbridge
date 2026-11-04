"""Append-only audit log (ADR 0032): storage, validation, redaction and the emitters.

`trustbridge.audit.credentials` (the credential events) is imported by name, not re-exported
here, because it depends on the adapter core's types.
"""

from trustbridge.audit.actions import Action, SystemActor
from trustbridge.audit.emit import (
    AuditDenials,
    AuditWriter,
    TenantSyncAudit,
    current_trace_id,
    with_trace,
)
from trustbridge.audit.export_event import record_export
from trustbridge.audit.model import (
    MAX_PAGE_SIZE,
    MAX_PAYLOAD_BYTES,
    SCHEMA_VERSION,
    TEXT_LIMITS,
    ActorType,
    AuditEvent,
    AuditPage,
    AuditQuery,
    AuditRecord,
    Outcome,
)
from trustbridge.audit.redact import ALLOW_LISTS, project, scrub_text, truncate
from trustbridge.audit.store import (
    append,
    export_ndjson,
    list_events,
    purge_batch,
    purge_expired,
)

__all__ = [
    "ALLOW_LISTS",
    "MAX_PAGE_SIZE",
    "MAX_PAYLOAD_BYTES",
    "SCHEMA_VERSION",
    "TEXT_LIMITS",
    "Action",
    "ActorType",
    "AuditDenials",
    "AuditEvent",
    "AuditPage",
    "AuditQuery",
    "AuditRecord",
    "AuditWriter",
    "Outcome",
    "SystemActor",
    "TenantSyncAudit",
    "append",
    "current_trace_id",
    "export_ndjson",
    "list_events",
    "project",
    "purge_batch",
    "purge_expired",
    "record_export",
    "scrub_text",
    "truncate",
    "with_trace",
]
