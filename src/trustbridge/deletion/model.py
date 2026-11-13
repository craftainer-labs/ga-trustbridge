"""Values of the deletion engine: the record, the lease, a step's outcome and the errors."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

STEP_STATES = ("draining", "platform", "github", "database")
"""The states that run a step, in order. `requested` only waits; `done` is written by finish."""
NEXT_STATE = {
    "requested": "draining",
    "draining": "platform",
    "platform": "github",
    "github": "database",
}
"""The state `tb_advance_tenant_deletion` allows after each one (no skipping, no going back)."""


class LeaseLost(Exception):  # noqa: N818 -- a condition, not an error of the caller's making
    """The deletion lease is gone (`TB003`) or the record is terminal or invalid (`TB004`): stop."""


class RunBudgetExhausted(Exception):  # noqa: N818 -- a condition, not an error
    """The run reached its budget (a fraction of the lease TTL): release the lease and stop."""


class NonRetryableError(Exception):
    """A step error that can never succeed on retry: the deletion is written `failed`.

    `code` is a short fixed identifier (it is recorded in the audit event); `message` is free
    text that is scrubbed before it reaches `last_error` and never reaches the audit trail.
    """

    def __init__(self, code: str, message: str = "") -> None:
        """Carry `code` (printable, at most 64 characters) and an optional `message`."""
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class Lease:
    """The proof every guarded transition carries: the deletion, the holder and the fence."""

    id: UUID
    holder: str
    fence: int


@dataclass(frozen=True, slots=True)
class DeletionRecord:
    """The part of a `tenant_deletions` row the engine needs (ids, flags, counts, no content)."""

    id: UUID
    tenant_id: UUID
    host: str
    installation_id: int
    delete_platform_resources: bool
    state: str
    step_cursor: Mapping[str, Any]
    counts: Mapping[str, int]
    skipped: Sequence[Mapping[str, Any]]
    fence: int
    attempts: int


@dataclass(frozen=True, slots=True)
class StepContext:
    """What a step receives: the record, the lease and `renew`.

    `renew` extends the lease and raises `RunBudgetExhausted` once the run's budget is spent, so a
    long step calls it between batches and stops cleanly. `LeaseLost` may come out of it too.
    """

    record: DeletionRecord
    lease: Lease
    renew: Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """What one pass of a step did.

    `counts` are added to the record's counts (rows or resources removed per category) and
    `skipped` entries (`category`, `reason` and optional scalars) are appended. With `done` false
    the engine saves `cursor` and runs the step again; true completes the step.
    """

    counts: Mapping[str, int] = field(default_factory=dict)
    skipped: Sequence[Mapping[str, Any]] = ()
    cursor: Mapping[str, Any] | None = None
    done: bool = True
