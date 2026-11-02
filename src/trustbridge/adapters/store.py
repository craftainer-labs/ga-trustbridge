"""What the adapter core persists, and the narrow interfaces it needs from the rest of the app.

The tables live in Postgres (`adapter_resources`, `adapter_credentials`,
`credential_operations`, ADR 0028); `memory.py` holds an in-memory implementation for unit
tests and `sql_store.py` the SQL one. The lease, the value store and the GitHub writer are
Protocols here because NG3 owns the lease and the secret-store backend and the GitHub core
owns the writer; the lander wires the real ones.
"""

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy import ColumnElement

from trustbridge.adapters.interface import (
    AdapterConfig,
    CredentialState,
    GitHubOutput,
    IssuedSecret,
    Permission,
    ResourceState,
    Tenant,
)

CredentialKind = Literal["oidc", "secret", "none"]


class OperationKind(StrEnum):
    """What a credential operation does."""

    ISSUE = "issue"
    ROTATE = "rotate"
    SWITCH = "switch"
    DELETE = "delete"


class OperationStatus(StrEnum):
    """Where a credential operation is. FINISHED, FAILED and ABANDONED are terminal."""

    INTENT = "intent"
    MANUAL = "needs_manual_action"
    PARKED = "parked"
    DELIVERED = "delivered"
    CLEANUP = "cleanup"
    FINISHED = "finished"
    FAILED = "failed"
    ABANDONED = "abandoned"


TERMINAL_STATUSES = frozenset(
    {OperationStatus.FINISHED, OperationStatus.FAILED, OperationStatus.ABANDONED}
)


@dataclass(frozen=True, slots=True)
class ScopeId:
    """The identity fields of a credential scope (the core derives key and trust offer)."""

    repository_id: int | None
    repository_owner_id: int
    environment: str | None
    permission: Permission


@dataclass(frozen=True, slots=True)
class DeliveredOutput:
    """A GitHub output the core wrote. Never holds a value."""

    kind: Literal["secret", "variable", "environment"]
    name: str
    repository_id: int | None
    environment: str | None


@dataclass(frozen=True, slots=True)
class StoredCredential:
    """One row of `adapter_credentials`."""

    tenant_id: UUID
    adapter: str
    scope_key: str
    scope: ScopeId
    kind: CredentialKind | None
    state: CredentialState
    outputs: tuple[DeliveredOutput, ...] = ()


@dataclass(frozen=True, slots=True)
class Operation:
    """One row of `credential_operations`: the write-ahead record of a secret-bearing call."""

    id: UUID
    tenant_id: UUID
    adapter: str
    scope_key: str
    kind: OperationKind
    status: OperationStatus
    oidc_offered: bool
    base_state: CredentialState | None
    base_kind: CredentialKind | None
    attempts: int = 0
    error: str | None = None
    manual_action: str | None = None

    @property
    def terminal(self) -> bool:
        """True once the operation can no longer progress."""
        return self.status in TERMINAL_STATUSES


class LeaseLostError(Exception):
    """A guarded write found that its lease expired or was taken over."""


class OutputError(Exception):
    """Writing or removing GitHub outputs failed. `retryable` says whether a retry may help."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        """Record the audit-safe message and whether a retry may help."""
        super().__init__(message)
        self.retryable = retryable


class Lease(Protocol):
    """A held lease (NG3). Guarded writes carry `guard()` in the same statement (invariant 10)."""

    @property
    def fence(self) -> int:
        """The fencing counter of this hold."""
        ...

    def guard(self) -> ColumnElement[bool]:
        """A SQL predicate that is true only while this hold is still the live lease."""
        ...


class LeaseProvider(Protocol):
    """Acquires the lease for `(tenant, adapter, key)` (NG3); one holder at a time."""

    def hold(
        self, *, tenant_id: UUID, adapter: str, key: str
    ) -> AbstractAsyncContextManager[Lease]:
        """Hold the lease for the duration of the context, or raise when it is busy."""
        ...


class CredentialStore(Protocol):
    """Persistence of credential state and write-ahead operations."""

    async def get_credential(
        self, tenant_id: UUID, adapter: str, scope_key: str
    ) -> StoredCredential | None:
        """Return the credential row, or None."""
        ...

    async def list_credentials(self, tenant_id: UUID, adapter: str) -> Sequence[StoredCredential]:
        """Return every credential row of the tenant for one adapter."""
        ...

    async def get_open_operation(
        self, tenant_id: UUID, adapter: str, scope_key: str
    ) -> Operation | None:
        """Return the scope's non-terminal operation, or None."""
        ...

    async def get_operation(self, operation_id: UUID) -> Operation | None:
        """Return an operation by id, or None."""
        ...

    async def list_open_operations(self) -> Sequence[Operation]:
        """Return every non-terminal operation (the reconciler resumes those)."""
        ...

    async def save(
        self, lease: Lease, credential: StoredCredential, operation: Operation | None
    ) -> None:
        """Atomically upsert the credential row and the operation row, guarded by the lease.

        Raises `LeaseLostError` when the guard is false.
        """
        ...

    async def count_slots(self, tenant_id: UUID, adapter: str) -> int:
        """Count current and previous slots (in-flight ones included) of the tenant's adapter."""
        ...

    async def known_names(self, tenant_id: UUID, adapter: str) -> frozenset[str]:
        """Return the names of all slots plus those of non-terminal operations (orphan sweep)."""
        ...

    async def get_resource(
        self, tenant_id: UUID, adapter: str, resource_key: str
    ) -> ResourceState | None:
        """Return a non-secret resource's state, or None."""
        ...

    async def put_resource(
        self,
        lease: Lease,
        tenant_id: UUID,
        adapter: str,
        resource_key: str,
        state: ResourceState | None,
    ) -> None:
        """Store (or with None, forget) a resource's state, guarded by the lease."""
        ...


class ValueStore(Protocol):
    """Parks secret values between issue and delivery. A secret-store backend, never Postgres."""

    async def put(self, operation_id: UUID, secret: IssuedSecret) -> None:
        """Park the values of an operation (encrypted at rest by the backend)."""
        ...

    async def get(self, operation_id: UUID) -> IssuedSecret | None:
        """Return the parked values, or None."""
        ...

    async def delete(self, operation_id: UUID) -> None:
        """Remove the parked values; absent is fine."""
        ...

    async def list_ids(self) -> Sequence[UUID]:
        """List the operation ids that have parked values (for the sweep)."""
        ...


class OutputWriter(Protocol):
    """Writes outputs to GitHub (secrets, variables, environments); the GitHub core is it."""

    async def apply(self, tenant: Tenant, outputs: Sequence[GitHubOutput]) -> None:
        """Create or update each output idempotently. Raises `OutputError`."""
        ...

    async def remove(self, tenant: Tenant, outputs: Sequence[DeliveredOutput]) -> None:
        """Delete each output; absent is fine. Raises `OutputError`."""
        ...


class ConfigResolver(Protocol):
    """Resolves a tenant's connection data for one adapter from the secret store (NG3/NG5)."""

    async def config_for(self, tenant: Tenant, adapter: str) -> AdapterConfig:
        """Return the adapter's per-tenant config."""
        ...


class TenantResolver(Protocol):
    """Resolves a surrogate tenant id to a `Tenant` (NG3's tenants table)."""

    async def get_tenant(self, tenant_id: UUID) -> Tenant:
        """Return the tenant."""
        ...


@dataclass(frozen=True, slots=True)
class CoreSettings:
    """Tuning of the adapter core. Defaults follow ADR 0028 decision 9."""

    lease_ttl_seconds: float = 120.0
    adapter_call_timeout_seconds: float = 30.0
    max_attempts: int = 5
    retire_grace_seconds: float = 3600.0

    def __post_init__(self) -> None:
        """Enforce invariant 11: the adapter call timeout is shorter than the lease TTL."""
        if self.adapter_call_timeout_seconds >= self.lease_ttl_seconds:
            msg = "adapter_call_timeout_seconds must be shorter than lease_ttl_seconds"
            raise ValueError(msg)
        if self.max_attempts < 1:
            msg = "max_attempts must be at least 1"
            raise ValueError(msg)


def now_utc_default() -> datetime:
    """Return the current UTC time (the default clock)."""
    return datetime.now(UTC)
