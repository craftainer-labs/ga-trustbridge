"""Narrow interfaces the GitHub core needs from the rest of the application.

NG3 owns the secret store, the tenants table and the settings; the lander wires real
implementations. Each Protocol here is the smallest surface the GitHub core uses.
"""

from collections.abc import Mapping, Sequence
from typing import Protocol
from uuid import UUID

from pydantic import SecretStr


class SecretReader(Protocol):
    """Reads a secret by reference from the secret store (Vault KV, Kubernetes Secret...)."""

    async def read(self, ref: str) -> SecretStr:
        """Return the secret, or raise when the store is unreachable or the ref is missing."""
        ...


class SecretWriter(Protocol):
    """Writes a secret to the secret store under a reference."""

    async def write(self, ref: str, value: SecretStr) -> None:
        """Store (or replace) the secret."""
        ...


class TenantConnectionStore(Protocol):
    """A tenant's per-adapter connection data (the Argo CD read-only App's key lives here)."""

    async def put(self, tenant_id: UUID, adapter: str, values: Mapping[str, SecretStr]) -> None:
        """Merge `values` into the tenant's connection data for `adapter`."""
        ...


class InstallationResolver(Protocol):
    """Maps a tenant id to its GitHub installation id (NG3's tenants table)."""

    async def installation_id(self, tenant_id: UUID) -> int:
        """Return the installation id of the tenant."""
        ...


class NonceStore(Protocol):
    """One-time values (manifest-flow `state` nonces); backed by Valkey or Postgres."""

    async def consume(self, nonce: str) -> bool:
        """Return True exactly once per issued nonce, then False."""
        ...

    async def issue(self, nonce: str, ttl_seconds: int) -> None:
        """Remember an issued nonce for `ttl_seconds`."""
        ...


class InstallationStore(Protocol):
    """Tenant bookkeeping driven by installation lifecycle events (NG3's tenants table)."""

    async def upsert_installation(
        self, host: str, installation_id: int, org: str, org_id: int
    ) -> UUID:
        """Create or update the tenant for `(host, installation_id)` and return its id."""
        ...

    async def tenant_id_for(self, host: str, installation_id: int) -> UUID | None:
        """Return the tenant id of `(host, installation_id)`, or None if unknown."""
        ...

    async def set_installation_state(
        self, host: str, installation_id: int, state: str
    ) -> UUID | None:
        """Set `active`, `suspended` or `deleted`; return the tenant id, or None if unknown."""
        ...

    async def apply_repository_changes(
        self,
        tenant_id: UUID,
        added: Sequence[tuple[int, str]],
        removed: Sequence[int],
    ) -> None:
        """Record repositories (id, full name) added to and ids removed from the installation."""
        ...

    async def rename_repository(self, tenant_id: UUID, repository_id: int, full_name: str) -> None:
        """Record a repository rename or transfer (identity is the numeric id)."""
        ...


class ReconcileScheduler(Protocol):
    """Asks the reconciler to look at a tenant soon (NG3)."""

    async def schedule(self, tenant_id: UUID) -> None:
        """Enqueue a reconcile for the tenant."""
        ...
