"""The one module the rest of trustbridge may import from `deletion` (an import-linter contract).

The reconciler (NG6h-4a-3) holds a `DeletionDriver` and never the engine: this module imports
nothing from the package and the package `__init__` re-exports nothing, so depending on it loads
no engine code (ADR 0034).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID


class DeletionDriver(Protocol):
    """Runs tenant deletions one claim at a time (the reconciler's singleton duty)."""

    async def run_once(self) -> bool:
        """Claim one runnable deletion and run it until it ends, stops or hits its budget.

        Returns True when a deletion was claimed (the caller may poll again at once) and False
        when none was runnable. It raises only for a failure of the record store itself, never
        for a step error: those are recorded on the deletion.
        """
        ...


@dataclass(frozen=True, slots=True)
class PurgeResult:
    """The outcome of one purge run: records removed, and eligible records left unprotected.

    `unprotected` counts finished records past the cut-off that have no tombstone: they are never
    deleted (the purge never deletes a `done` record whose tombstone is missing); the duty logs
    them.
    """

    purged: int = 0
    unprotected: int = 0


class RecordPurger(Protocol):
    """Purges finished deletion records past `DELETION_RETENTION_DAYS` (the reconciler's duty).

    The hook the composition root (`trustbridge.composition`) fills: `ReconcilerDeps.record_purger`.
    With no purger the duty does nothing.
    """

    async def purge(self, on_batch: Callable[[int], None] | None = None) -> PurgeResult:
        """Run bounded batches; `on_batch(purged)` is called after each committed batch."""
        ...


@dataclass(frozen=True, slots=True)
class DeletionWiring:
    """What the composition root hands the reconciler: the engine and the record purger.

    Either may be None (mock mode composes nothing); the reconciler then logs that the duty is
    disabled and runs without it.
    """

    driver: DeletionDriver | None = None
    record_purger: RecordPurger | None = None


ItemKind = Literal["credential", "webhook", "repository", "project"]


@dataclass(frozen=True, slots=True)
class PlatformItem:
    """One recorded platform-side thing: a credential scope or a resource row.

    `key` is the scope key (credential) or the resource's own key (webhook, repository, project).
    """

    kind: ItemKind
    adapter: str
    key: str


class TrackedEnvironments(Protocol):
    """The GitHub environments trustbridge created for a tenant (NG6h-6a; used by NG6h-6).

    ADR 0034 user decision B6: the GitHub step deletes exactly these and no other environment; an
    environment with no row here is kept and recorded as `environment_kept`. Implemented by
    `trustbridge.github.environments.SqlEnvironmentStore`; the packages do not import each other.
    """

    async def tracked(self, tenant_id: UUID) -> Sequence[tuple[int, str]]:
        """The tenant's tracked environments as `(repository_id, name)`, in key order."""
        ...

    async def tracked_since(self, tenant_id: UUID) -> Sequence[tuple[int, str, datetime]]:
        """As `tracked`, with each row's `created_at` (NG6h-6's recreation guard)."""
        ...

    async def forget(self, tenant_id: UUID, repository_id: int, name: str) -> None:
        """Remove one row once its environment is deleted (or already gone); absent is fine."""
        ...


@dataclass(frozen=True, slots=True)
class GithubOutput:
    """One recorded GitHub secret or variable: an entry of a credential row's `outputs`.

    Names and ids only (never a value). `adapter` and `scope_key` say which credential row records
    it, so the entry can be cleared from that row once it is removed from GitHub.
    """

    adapter: str
    scope_key: str
    kind: Literal["secret", "variable"]
    name: str
    repository_id: int | None
    environment: str | None


@dataclass(frozen=True, slots=True)
class TrackedEnvironment:
    """An environment trustbridge created (a `github_environments` row) and when it did."""

    repository_id: int
    name: str
    created_at: datetime
