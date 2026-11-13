"""The deletion engine: state machine, lease and fence loop, failure policy (ADR 0034).

One `run_once` claims one runnable deletion and walks it along `requested > draining > platform >
github > database > done`, each step idempotent and resumable from its saved cursor. Every
transition goes through the record store under the live lease, so a holder whose lease lapsed
cannot commit a step (`LeaseLost`, from `TB003` and `TB004` alike: stop this deletion now).

* **Budget.** The run ends, releasing the lease, once `budget_fraction` of the lease TTL has gone
  (about half), so it stops before the lease could lapse: renewals are unbounded by design.
* **Failure.** Any exception from a step or the store is transient: the engine records it, counts
  the attempt (`trustbridge.deletion.attempts`), backs off `min(cap, base * 2**attempts)` and frees
  the lease. A `NonRetryableError` is the only way to `failed`. A compliance deletion never gives
  up silently.
"""

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from trustbridge.audit import AuditEvent, scrub_text
from trustbridge.deletion import audit, metrics
from trustbridge.deletion.model import (
    NEXT_STATE,
    DeletionRecord,
    Lease,
    LeaseLost,
    NonRetryableError,
    RunBudgetExhausted,
    StepContext,
)
from trustbridge.deletion.steps import DatabaseStep, GithubStep, PlatformStep, Step
from trustbridge.deletion.store import DeletionStore

logger = logging.getLogger(__name__)
MAX_TTL_SECONDS = 3600
MAX_DELAY_SECONDS = 86400
MAX_ERROR_CHARS = 500


@dataclass(slots=True)
class _Run:
    """The state of one run: the record as last committed, which the error paths report on."""

    record: DeletionRecord
    lease: Lease
    deadline: float


@dataclass(frozen=True, slots=True)
class EngineSteps:
    """The four step slots. `drain` is `deletion.drain.DrainStep` in production."""

    drain: Step
    platform: PlatformStep
    github: GithubStep
    database: DatabaseStep


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """The engine's timing (the settings arrive in NG6h-4a-3)."""

    holder: str
    lease_ttl_seconds: int
    backoff_base_seconds: int
    backoff_cap_seconds: int
    budget_fraction: float = 0.5

    def __post_init__(self) -> None:
        """Reject values the database functions or the budget logic cannot honour."""
        if not 1 <= self.lease_ttl_seconds <= MAX_TTL_SECONDS:
            raise ValueError("lease_ttl_seconds must be from 1 to 3600")
        if not 0 <= self.backoff_base_seconds <= self.backoff_cap_seconds <= MAX_DELAY_SECONDS:
            raise ValueError("backoff must satisfy 0 <= base <= cap <= 86400 seconds")
        if not 0 < self.budget_fraction < 1:
            raise ValueError("budget_fraction must be between 0 and 1 (exclusive)")
        if not 1 <= len(self.holder) <= 255:  # noqa: PLR2004 -- the column limit of the functions
            raise ValueError("holder must be 1 to 255 characters")


def _merge_counts(base: Mapping[str, int], more: Mapping[str, int]) -> dict[str, int]:
    out = dict(base)
    for key, value in more.items():
        out[key] = out.get(key, 0) + value
    return out


def _describe(error: Exception) -> str:
    """`last_error` for a transient failure: the class and the scrubbed, cut message."""
    return f"{type(error).__name__}: {scrub_text(str(error), MAX_ERROR_CHARS)}"[:MAX_ERROR_CHARS]


class DeletionEngine:
    """Implements `DeletionDriver` (`deletion.ports`) over a record store and the step slots."""

    def __init__(
        self,
        store: DeletionStore,
        steps: EngineSteps,
        config: EngineConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Drive `store` with `steps`; `clock` (monotonic seconds) is injectable for tests."""
        self._store = store
        self._steps = steps
        self._config = config
        self._clock = clock

    async def run_once(self) -> bool:
        """Claim one runnable deletion and run it; False when there was none."""
        record = await self._store.claim(self._config.holder, self._config.lease_ttl_seconds)
        if record is None:
            return False
        run = _Run(
            record,
            Lease(record.id, self._config.holder, record.fence),
            self._clock() + self._config.lease_ttl_seconds * self._config.budget_fraction,
        )
        try:
            await self._walk(run)
        except LeaseLost:
            logger.warning("tenant deletion %s: lease lost, stopping", record.id)
        except RunBudgetExhausted:
            await self._release(run.lease)
        except NonRetryableError as error:
            await self._fail(run, error)
        except Exception as error:
            await self._retry(run, error)
        return True

    async def _renew(self, run: _Run) -> None:
        if self._clock() >= run.deadline:
            raise RunBudgetExhausted
        await self._store.renew(run.lease, self._config.lease_ttl_seconds)

    def _step_for(self, state: str) -> Step:
        return {
            "draining": self._steps.drain,
            "platform": self._steps.platform,
            "github": self._steps.github,
            "database": self._steps.database,
        }[state]

    async def _walk(self, run: _Run) -> None:
        """Advance until `done`, a stop, or the budget; the caller maps what is raised."""
        while True:
            await self._renew(run)
            record = run.record
            if record.state == "requested":
                # Draining is the first destructive step, so this transition is audited too.
                events = [audit.step_event(record, "draining", record.counts)]
                await self._advance(run, "draining", record.counts, record.skipped, events)
                continue
            outcome = await self._step_for(record.state).run(
                StepContext(record, run.lease, lambda: self._renew(run))
            )
            counts = _merge_counts(record.counts, outcome.counts)
            skipped = [*record.skipped, *outcome.skipped]
            if not outcome.done:
                await self._advance(run, record.state, counts, skipped, [], cursor=outcome.cursor)
                continue
            events = audit.skipped_events(record, outcome.skipped)
            if record.state == "database":
                events += [
                    audit.step_event(record, "done", counts),
                    audit.completed_event(record, counts),
                ]
                await self._store.finish(
                    run.lease, record, counts=counts, skipped=skipped, events=events
                )
                return
            to_state = NEXT_STATE[record.state]
            events.insert(0, audit.step_event(record, to_state, counts))
            await self._advance(run, to_state, counts, skipped, events)

    async def _advance(  # noqa: PLR0913 -- one transition's data, kept explicit
        self,
        run: _Run,
        to_state: str,
        counts: Mapping[str, int],
        skipped: Sequence[Mapping[str, Any]],
        events: Sequence[AuditEvent],
        *,
        cursor: Mapping[str, Any] | None = None,
    ) -> None:
        """Commit the transition and its events, then move `run.record` to what was committed."""
        record = run.record
        await self._store.advance(
            run.lease, to_state, cursor=cursor, counts=counts, skipped=skipped, events=events
        )
        changed = to_state != record.state
        run.record = replace(
            record,
            state=to_state,
            counts=counts,
            skipped=skipped,
            step_cursor=cursor if cursor is not None else ({} if changed else record.step_cursor),
            attempts=0 if changed else record.attempts,
        )

    async def _release(self, lease: Lease) -> None:
        try:
            await self._store.release(lease)
        except LeaseLost:
            logger.warning("tenant deletion %s: lease lost before release", lease.id)

    async def _fail(self, run: _Run, error: NonRetryableError) -> None:
        reason = error.code
        if error.message:
            reason = f"{error.code}: {scrub_text(error.message, MAX_ERROR_CHARS)}"
        metrics.failed.add(1, {"step": run.record.state})
        try:
            await self._store.fail(run.lease, reason, [audit.failed_event(run.record, error.code)])
        except LeaseLost:
            logger.warning("tenant deletion %s: lease lost before failing", run.record.id)

    async def _retry(self, run: _Run, error: Exception) -> None:
        cfg = self._config
        record = run.record
        delay = min(
            cfg.backoff_cap_seconds, cfg.backoff_base_seconds * 2 ** min(record.attempts, 30)
        )
        metrics.attempts.add(1, {"step": record.state})
        logger.warning(
            "tenant deletion %s: %s step failed (%s), retrying in %ss",
            record.id,
            record.state,
            type(error).__name__,
            delay,
        )
        try:
            await self._store.retry(run.lease, _describe(error), delay)
        except LeaseLost:
            logger.warning("tenant deletion %s: lease lost before retry", record.id)
