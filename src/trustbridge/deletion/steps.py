"""The step slots of the engine (ADR 0034 "Steps"). 4b, 5 and 6 fill them; 4a-2 uses doubles.

Each step is idempotent: the engine may run it again after a crash or a lost lease, from the
cursor of its last saved pass. A transient failure is any exception; `NonRetryableError` makes
the deletion `failed`. A step that cannot complete holds the deletion in its state (retry with
backoff): it never advances on its own (user decision 2).
"""

from typing import Protocol
from uuid import UUID

from trustbridge.deletion.model import StepContext, StepOutcome


class Step(Protocol):
    """One resumable pass of a step."""

    async def run(self, ctx: StepContext) -> StepOutcome:
        """Run one pass from `ctx.record.step_cursor`; `done` false asks for another pass."""
        ...


class PlatformStep(Step, Protocol):
    """Step 2: adapter delete and revoke operations through the write-ahead path (NG6h-5)."""


class GithubStep(Step, Protocol):
    """Step 3: remove the secrets, variables and environments trustbridge wrote (NG6h-6)."""


class DatabaseStep(Step, Protocol):
    """Step 5, the batches: clear the tenant's rows (`SqlDatabaseStep`, NG6h-4b).

    It deletes every tenant-keyed row and the webhook bodies but NOT the `tenants` row: the
    engine removes that last, through the fenced `tb_delete_tenant`, in the transaction that also
    finishes the record and writes `tenant.deletion.completed`.
    """


class WritersPort(Protocol):
    """What the drain step needs to find and stop the tenant's writers."""

    async def quiesce(self, tenant_id: UUID) -> int:
        """Scan once and return how many writers are still active for the tenant.

        The scan covers `webhook_deliveries` recorded for the tenant and its
        `credential_operations` (abandoning in-flight ones under the normal rules) and
        leases still held. Zero means a quiescent pass.
        """
        ...
