"""The one module the rest of trustbridge may import from `deletion` (an import-linter contract).

The reconciler (NG6h-4a-3) holds a `DeletionDriver` and never the engine: this module imports
nothing from the package and the package `__init__` re-exports nothing, so depending on it loads
no engine code (ADR 0034).
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


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
    deleted (the purge cannot remove the last tombstone record) and the duty logs them.
    """

    purged: int = 0
    unprotected: int = 0


class RecordPurger(Protocol):
    """Purges finished deletion records past `DELETION_RETENTION_DAYS` (the reconciler's duty).

    The hook the composition root (NG6h-7) fills: `ReconcilerDeps.record_purger`. With no purger
    the duty does nothing.
    """

    async def purge(self, on_batch: Callable[[int], None] | None = None) -> PurgeResult:
        """Run bounded batches; `on_batch(purged)` is called after each committed batch."""
        ...
