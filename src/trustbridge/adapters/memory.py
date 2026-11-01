"""In-memory implementations of the core's persistence and lease interfaces.

For unit tests and `MODE=mock` (ADR 0006): no Postgres, no secret store. The lease here
serialises with `asyncio.Lock`s and its guard is always true, so lease-fencing itself is
exercised by the SQL store's integration tests, not here.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import ColumnElement, true

from trustbridge.adapters.interface import GitHubOutput, IssuedSecret, ResourceState, Tenant
from trustbridge.adapters.store import (
    DeliveredOutput,
    Lease,
    LeaseLostError,
    Operation,
    StoredCredential,
)


@dataclass(frozen=True, slots=True)
class MemoryLease:
    """A lease whose guard is always true, unless `alive` is switched off by a test."""

    fence: int
    holder: MemoryLeases
    key: tuple[UUID, str, str]

    def guard(self) -> ColumnElement[bool]:
        """Return a SQL predicate; unused by the in-memory store, which checks `alive`."""
        return true()

    @property
    def alive(self) -> bool:
        """True while this hold is the current one for its key."""
        return self.holder.fences.get(self.key) == self.fence


class MemoryLeases:
    """`LeaseProvider` backed by per-key asyncio locks and a fence counter."""

    def __init__(self) -> None:
        """Start with no leases."""
        self.fences: dict[tuple[UUID, str, str], int] = {}
        self._locks: dict[tuple[UUID, str, str], asyncio.Lock] = {}
        self.held: list[tuple[UUID, str, str]] = []

    @asynccontextmanager
    async def hold(self, *, tenant_id: UUID, adapter: str, key: str) -> AsyncIterator[Lease]:
        """Hold the lease for `(tenant, adapter, key)`, waiting if another task holds it."""
        k = (tenant_id, adapter, key)
        lock = self._locks.setdefault(k, asyncio.Lock())
        async with lock:
            fence = self.fences.get(k, 0) + 1
            self.fences[k] = fence
            self.held.append(k)
            yield MemoryLease(fence, self, k)


class InMemoryStore:
    """`CredentialStore` over dicts."""

    def __init__(self) -> None:
        """Start empty."""
        self.credentials: dict[tuple[UUID, str, str], StoredCredential] = {}
        self.operations: dict[UUID, Operation] = {}
        self.resources: dict[tuple[UUID, str, str], ResourceState] = {}
        self.before_known_names: Callable[[], Awaitable[None]] | None = None

    def _check(self, lease: Lease) -> None:
        if isinstance(lease, MemoryLease) and not lease.alive:
            msg = "lease lost"
            raise LeaseLostError(msg)

    async def get_credential(
        self, tenant_id: UUID, adapter: str, scope_key: str
    ) -> StoredCredential | None:
        """Return the credential row, or None."""
        return self.credentials.get((tenant_id, adapter, scope_key))

    async def list_credentials(self, tenant_id: UUID, adapter: str) -> Sequence[StoredCredential]:
        """Return every credential row of the tenant for one adapter."""
        return [
            c
            for c in self.credentials.values()
            if c.tenant_id == tenant_id and c.adapter == adapter
        ]

    async def get_open_operation(
        self, tenant_id: UUID, adapter: str, scope_key: str
    ) -> Operation | None:
        """Return the scope's non-terminal operation, or None."""
        for op in self.operations.values():
            if (op.tenant_id, op.adapter, op.scope_key) == (
                tenant_id,
                adapter,
                scope_key,
            ) and not op.terminal:
                return op
        return None

    async def get_operation(self, operation_id: UUID) -> Operation | None:
        """Return an operation by id, or None."""
        return self.operations.get(operation_id)

    async def list_open_operations(self) -> Sequence[Operation]:
        """Return every non-terminal operation."""
        return [op for op in self.operations.values() if not op.terminal]

    async def save(
        self, lease: Lease, credential: StoredCredential, operation: Operation | None
    ) -> None:
        """Upsert the credential row and the operation row."""
        self._check(lease)
        key = (credential.tenant_id, credential.adapter, credential.scope_key)
        self.credentials[key] = credential
        if operation is not None:
            self.operations[operation.id] = operation

    async def count_slots(self, tenant_id: UUID, adapter: str) -> int:
        """Count current and previous slots of the tenant's adapter."""
        total = 0
        for cred in await self.list_credentials(tenant_id, adapter):
            total += sum(1 for s in (cred.state.current, cred.state.previous) if s is not None)
        return total

    async def known_names(self, tenant_id: UUID, adapter: str) -> frozenset[str]:
        """Return the names of all slots (open operations' slots are in the rows)."""
        if self.before_known_names is not None:
            await self.before_known_names()
        names: set[str] = set()
        for cred in await self.list_credentials(tenant_id, adapter):
            for slot in (cred.state.current, cred.state.previous):
                if slot is not None:
                    names.add(slot.name)
        return frozenset(names)

    async def get_resource(
        self, tenant_id: UUID, adapter: str, resource_key: str
    ) -> ResourceState | None:
        """Return a resource's state, or None."""
        return self.resources.get((tenant_id, adapter, resource_key))

    async def put_resource(
        self,
        lease: Lease,
        tenant_id: UUID,
        adapter: str,
        resource_key: str,
        state: ResourceState | None,
    ) -> None:
        """Store (or with None, forget) a resource's state."""
        self._check(lease)
        key = (tenant_id, adapter, resource_key)
        if state is None:
            self.resources.pop(key, None)
        else:
            self.resources[key] = state


class InMemoryValueStore:
    """`ValueStore` over a dict."""

    def __init__(self) -> None:
        """Start empty."""
        self.parked: dict[UUID, IssuedSecret] = {}

    async def put(self, operation_id: UUID, secret: IssuedSecret) -> None:
        """Park the values of an operation."""
        self.parked[operation_id] = secret

    async def get(self, operation_id: UUID) -> IssuedSecret | None:
        """Return the parked values, or None."""
        return self.parked.get(operation_id)

    async def delete(self, operation_id: UUID) -> None:
        """Remove the parked values; absent is fine."""
        self.parked.pop(operation_id, None)

    async def list_ids(self) -> Sequence[UUID]:
        """List the operation ids that have parked values."""
        return list(self.parked)


class RecordingWriter:
    """`OutputWriter` that records what the core wrote (tests)."""

    def __init__(self) -> None:
        """Start with nothing written."""
        self.applied: list[GitHubOutput] = []
        self.removed: list[DeliveredOutput] = []
        self.fail_next: list[Exception] = []

    async def apply(self, tenant: Tenant, outputs: Sequence[GitHubOutput]) -> None:
        """Record the outputs, or raise a queued failure."""
        del tenant
        if self.fail_next:
            raise self.fail_next.pop(0)
        self.applied.extend(outputs)

    async def remove(self, tenant: Tenant, outputs: Sequence[DeliveredOutput]) -> None:
        """Record the removals."""
        del tenant
        self.removed.extend(outputs)
