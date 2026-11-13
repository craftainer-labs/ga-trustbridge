"""The record store the engine drives: the definer functions of migration `5d7a2c8e1b94`."""

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from trustbridge.audit import AuditEvent
from trustbridge.deletion.model import DeletionRecord, Lease


class DeletionStore(Protocol):
    """Every method that writes does so under the live lease or raises `LeaseLost`.

    A transition and the audit events passed with it are one transaction: both commit or neither.
    """

    async def claim(self, holder: str, ttl_seconds: int) -> DeletionRecord | None:
        """Take the lease of one runnable deletion (fence bumped), or None."""
        ...

    async def renew(self, lease: Lease, ttl_seconds: int) -> None:
        """Extend the lease."""
        ...

    async def release(self, lease: Lease) -> None:
        """Free the lease, keeping the state."""
        ...

    async def retry(self, lease: Lease, error: str, delay_seconds: int) -> int:
        """Record `error`, count the attempt, back off and free the lease; returns the attempts."""
        ...

    async def advance(  # noqa: PLR0913 -- the transition's own data, keyword-only
        self,
        lease: Lease,
        to_state: str,
        *,
        cursor: Mapping[str, Any] | None,
        counts: Mapping[str, int],
        skipped: Sequence[Mapping[str, Any]],
        events: Sequence[AuditEvent],
    ) -> None:
        """Move one step along (or, with the same state, save the cursor) and write `events`."""
        ...

    async def finish(
        self,
        lease: Lease,
        record: DeletionRecord,
        *,
        counts: Mapping[str, int],
        skipped: Sequence[Mapping[str, Any]],
        events: Sequence[AuditEvent],
    ) -> None:
        """Remove the `tenants` row (fenced), write `done` and `events`, in one transaction."""
        ...

    async def fail(self, lease: Lease, reason: str, events: Sequence[AuditEvent]) -> None:
        """Write the terminal `failed` state and `events`."""
        ...
