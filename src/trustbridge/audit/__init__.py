"""Append-only audit log, storage layer (ADR 0032); emitters (NG6f) and the API (NG6d) use it."""

from trustbridge.audit.model import (
    MAX_PAGE_SIZE,
    SCHEMA_VERSION,
    ActorType,
    AuditEvent,
    AuditPage,
    AuditQuery,
    AuditRecord,
    Outcome,
)
from trustbridge.audit.store import (
    append,
    export_ndjson,
    list_events,
    purge_batch,
    purge_expired,
)

__all__ = [
    "MAX_PAGE_SIZE",
    "SCHEMA_VERSION",
    "ActorType",
    "AuditEvent",
    "AuditPage",
    "AuditQuery",
    "AuditRecord",
    "Outcome",
    "append",
    "export_ndjson",
    "list_events",
    "purge_batch",
    "purge_expired",
]
