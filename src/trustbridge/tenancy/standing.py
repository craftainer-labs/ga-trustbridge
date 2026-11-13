"""The tenant standing the worker reads before handling a delivery (ADR 0033, ADR 0034).

Lives in `tenancy`, below `worker`, so `tenancy.gate` can return it without importing the worker.
"""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class TenantStanding:
    """What the worker needs to know about the tenant of an installation.

    `tenant_id` is None when no tenant row exists. `disabled` is the admin disable (ADR 0033).
    `refusal` is `tombstone` (a finished deletion of the key: nothing is handled and no tenant
    is created), `deleting` (a deletion is in progress), or None. A `deleted` installation
    state is not a refusal here: the lifecycle handlers deal with it.
    """

    tenant_id: UUID | None = None
    disabled: bool = False
    refusal: str | None = None
