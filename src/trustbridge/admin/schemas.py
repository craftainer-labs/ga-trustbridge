"""Response models of the admin API v1 (one per resource, mirroring the table columns).

NG6d decision A1: snake_case, UUIDs as strings, RFC 3339 timestamps (UTC), no secrets or
binary data. v1 is additive: new fields may appear, enum-like strings are open sets.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ProblemOut(BaseModel):
    """An RFC 9457 problem document (ADR 0004); `type` is `tag:trustbridge,2026:problem/<class>`."""

    type: str
    title: str
    status: int
    detail: str | list[Any]
    instance: str


class GrantOut(BaseModel):
    """One role grant of the caller."""

    role: str
    scope: Literal["instance", "tenant"]
    host: str | None = None
    org: str | None = None


class TenantRefOut(BaseModel):
    """A tenant the caller holds some role on."""

    id: UUID
    host: str
    org: str | None


class MeOut(BaseModel):
    """The authenticated caller (`GET /me`)."""

    sub: str
    iss: str | None
    grants: list[GrantOut]
    tenants: list[TenantRefOut]


class HostOut(BaseModel):
    """A configured GitHub host as the answering replica sees it (ADR 0030)."""

    name: str
    state: str
    reason: str | None


class HostPage(BaseModel):
    """A page of hosts."""

    items: list[HostOut]
    next_cursor: str | None


class TenantOut(BaseModel):
    """A tenant: one GitHub installation on one host.

    `state` is the installation state written by webhook sync; `admin_disabled_at` is the
    separate admin-disable column (ADR 0033 "Tenant disable"); `effective_state` combines them:
    `disabled` when disabled, otherwise `state`.
    """

    id: UUID
    host: str
    installation_id: int
    org: str | None
    org_id: int
    state: str
    admin_disabled_at: datetime | None
    effective_state: str
    deleting: bool


class TenantPage(BaseModel):
    """A page of tenants."""

    items: list[TenantOut]
    next_cursor: str | None


class AuditEventOut(BaseModel):
    """One audit event (ADR 0032): the same columns, in the same order, as an NDJSON line."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    seq: int
    occurred_at: datetime
    recorded_at: datetime
    tenant_id: UUID | None
    actor_type: str
    actor_sub: str
    actor_iss: str | None
    on_behalf_of: dict[str, Any] | None
    source_ip: str | None
    action: str
    resource_type: str
    resource_id: str | None
    outcome: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    request_id: str | None
    trace_id: str | None
    schema_version: int


class AuditEventPage(BaseModel):
    """A page of audit events."""

    items: list[AuditEventOut]
    next_cursor: str | None
