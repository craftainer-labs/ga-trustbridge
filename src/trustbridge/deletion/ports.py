"""The one module the rest of trustbridge may import from `deletion` (an import-linter contract).

The reconciler (NG6h-4a-3) holds a `DeletionDriver` and never the engine: this module imports
nothing from the package and the package `__init__` re-exports nothing, so depending on it loads
no engine code (ADR 0034).
"""

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
