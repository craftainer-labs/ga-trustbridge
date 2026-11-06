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
    """The authenticated caller (`GET /me`).

    `tenants` holds at most 200; `tenants_truncated` is true when more exist.
    """

    sub: str
    iss: str | None
    grants: list[GrantOut]
    tenants: list[TenantRefOut]
    tenants_truncated: bool = False


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
    `disabled` when disabled, otherwise `state`. `admin_disabled_by` (the actor's `sub`) and
    `admin_disable_reason` are null until the disable action exists (NG6e adds the columns).
    """

    id: UUID
    host: str
    installation_id: int
    org: str | None
    org_id: int
    state: str
    admin_disabled_at: datetime | None
    admin_disabled_by: str | None = None
    admin_disable_reason: str | None = None
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


class RepositoryOut(BaseModel):
    """A repository the tenant's installation can see (`tenant_repositories`)."""

    tenant_id: UUID
    repository_id: int
    full_name: str


class RepositoryPage(BaseModel):
    """A page of repositories."""

    items: list[RepositoryOut]
    next_cursor: str | None


class ResourceOut(BaseModel):
    """A non-secret adapter resource (`adapter_resources`)."""

    tenant_id: UUID
    adapter: str
    resource_key: str
    state: dict[str, Any]
    updated_at: datetime


class ResourcePage(BaseModel):
    """A page of resources."""

    items: list[ResourceOut]
    next_cursor: str | None


class CredentialOut(BaseModel):
    """The state of one credential scope (`adapter_credentials`): names and refs, never a value.

    `state` holds the current and previous slot (generation, name, platform ref, delivered flag,
    retire time); `outputs` the names of what was delivered to GitHub.
    """

    tenant_id: UUID
    adapter: str
    scope_key: str
    scope: dict[str, Any]
    kind: str | None
    state: dict[str, Any]
    outputs: list[dict[str, Any]]
    updated_at: datetime


class CredentialPage(BaseModel):
    """A page of credentials."""

    items: list[CredentialOut]
    next_cursor: str | None


class OperationOut(BaseModel):
    """A credential operation (`credential_operations`), newest first."""

    id: UUID
    tenant_id: UUID
    adapter: str
    scope_key: str
    kind: str
    status: str
    oidc_offered: bool
    base_state: dict[str, Any] | None
    base_kind: str | None
    attempts: int
    error: str | None
    manual_action: str | None
    created_at: datetime
    updated_at: datetime


class OperationPage(BaseModel):
    """A page of operations."""

    items: list[OperationOut]
    next_cursor: str | None


class DeliveryOut(BaseModel):
    """A received webhook delivery (`webhook_deliveries`) without its headers and body."""

    host: str
    delivery_id: str
    event: str
    status: str
    attempts: int
    received_at: datetime
    next_attempt_at: datetime | None
    last_relayed_at: datetime | None
    processed_at: datetime | None
    dead_at: datetime | None
    last_error: str | None
    body_sha256: str | None
    has_body: bool


class DeliveryDetailOut(DeliveryOut):
    """One delivery. `payload` is the stored body parsed as JSON, only for an `operator`.

    Never the headers. Null for a `viewer`, after the body was purged (retention) or when the
    stored body is not JSON.
    """

    payload: Any | None = None


class DeliveryPage(BaseModel):
    """A page of deliveries, newest first."""

    items: list[DeliveryOut]
    next_cursor: str | None


class WebhookCapabilityOut(BaseModel):
    """How a platform's inbound webhooks are authenticated (never a secret)."""

    auth: str
    header: str | None
    trigger_only: bool


class CapabilitiesOut(BaseModel):
    """What an adapter can do (ADR 0029 `Capabilities`); sets are sorted lists."""

    oidc: bool
    oidc_hosts: list[str]
    oidc_requires_manual_step: bool
    ci_credential: bool
    rotation: str
    webhook: WebhookCapabilityOut | None
    github_hosts: list[str]
    needs_privileged_credential: bool
    credential_limit: int | None


class AdapterOut(BaseModel):
    """An enabled adapter for one tenant: whether connection data is stored, and what it can do.

    Never the connection data itself (ADR 0033 invariant 8). `capabilities` is null only when
    the adapter cannot describe itself.
    """

    name: str
    configured: bool
    capabilities: CapabilitiesOut | None


class AdapterPage(BaseModel):
    """A page of adapters."""

    items: list[AdapterOut]
    next_cursor: str | None
