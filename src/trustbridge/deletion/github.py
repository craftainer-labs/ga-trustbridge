"""The GitHub step of a tenant deletion (ADR 0034 step 3; NG6h-6).

Removes what trustbridge wrote to GitHub for the tenant, and nothing else: the secrets and
variables recorded in the tenant's credential rows (`outputs`), then the environments trustbridge
created (the `github_environments` rows, decision B6). It works over a `GithubPort` (the worker's
`SqlGithubPort` in production, which removes through `GitHubOutputWriter.remove` and the tenant
gateway with an installation token). It imports nothing from `adapters` or `github` (a contract).

**The installation is checked first, live** (decision B9; ADR 0034 invariant 9). When
`installation_exists` says the App is gone, GitHub has already revoked access and no token can be
minted, so the step makes no other GitHub call: it records the recorded outputs and tracked
environments as `installation_gone` skipped entries (categories `github_outputs` and
`github_environments`, with the repository ids and a count, never a name) and finishes. With
nothing recorded at all it makes no call. A lookup that fails (host not ready, GitHub down) is
transient like any other.

**Environments (B6).** Only a tracked environment is ever deleted. Before deleting it, GitHub's
`created_at` is compared with the row's: when GitHub's is later than the row's plus
`CREATED_AT_SKEW`, someone deleted our environment and made their own of the same name, so it is
kept, recorded as `environment_kept` (reason `recreated`) and the row is forgotten. An environment
GitHub no longer has (404) counts as done. An environment named only by a credential row's
`environment` output, with no tracking row, is never deleted; the last pass records how many as
`environment_kept` (reason `untracked`).

**Pass semantics** are the platform step's (NG6h-5/5b): the work list is the recorded secrets and
variables, then the tracked environments, re-read from the rows on every pass (so a crash between a
removal and the saved cursor converges: a removed item is no longer listed); every remaining item is
tried once per pass; the start rotates by `record.attempts` so items stuck at the head cannot starve
the rest; progress (a changed cursor, which resets the engine's attempts) is reported when anything
was removed or settled; only when every remaining item failed does the step raise
`GithubStepHeldError` (the engine retries with backoff, counts the attempt and alerts). Failures are
logged by exception class only. `LeaseLost`, `RunBudgetExhausted` and `NonRetryableError` propagate.
As with the platform step, counts and the `recreated` skipped entry of a pass are saved only when
the pass ends, so a crash can lose that pass's.
"""

import logging
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from trustbridge.deletion.model import (
    LeaseLost,
    NonRetryableError,
    RunBudgetExhausted,
    StepContext,
    StepOutcome,
)
from trustbridge.deletion.ports import GithubOutput, TrackedEnvironment

logger = logging.getLogger(__name__)

MAX_TRIES_PER_PASS = 50
"""Bound on removals plus failures tried in one pass (the run budget bounds it in time too)."""
CREATED_AT_SKEW = timedelta(minutes=5)
"""How much later than the row GitHub's `created_at` may be and still count as ours.

The row is written after the writer's `PUT` returns, from the database clock, so GitHub's
`created_at` normally precedes the row's by one round trip. The margin only has to cover the two
clocks disagreeing: GitHub.com and an NTP-synced database differ by well under a second, a
self-hosted GHES by the drift its operator allows. Five minutes covers that with room to spare and
stays far below the time a person needs to notice a missing environment, delete ours (it is
already gone) and create their own, which is the case the guard exists for. A margin too small
fails to the safe side (our own environment is kept), too large to the unsafe one, so it is no
larger than it needs to be.
"""

COUNT_OUTPUTS = "github_outputs_removed"
COUNT_ENVIRONMENTS = "github_environments_removed"
"""Names in `counts` (they avoid the secret words of the audit shapes and the database step's
`github_environments`, which counts the rows it deletes)."""
SKIPPED_OUTPUTS = "github_outputs"
SKIPPED_ENVIRONMENTS = "github_environments"
GONE_REASON = "installation_gone"
KEPT_CATEGORY = "environment_kept"
RECREATED_REASON = "recreated"
UNTRACKED_REASON = "untracked"


class GithubStepHeldError(Exception):
    """Every remaining item failed in this pass: the engine retries, backs off and alerts.

    The message carries counts and exception class names only, never a message, name or value.
    """


class GithubPort(Protocol):
    """The recorded GitHub-side state of a tenant, and the calls that remove it."""

    async def installation_exists(self, host: str, installation_id: int) -> bool:
        """Whether the App installation still exists, read live (one cheap App-JWT call)."""
        ...

    async def outputs(self, tenant_id: UUID) -> Sequence[GithubOutput]:
        """The secrets and variables still recorded in the tenant's credential rows."""
        ...

    async def remove_output(self, tenant_id: UUID, output: GithubOutput) -> None:
        """Delete it from GitHub (absent is fine), then clear it from its credential row."""
        ...

    async def environments(self, tenant_id: UUID) -> Sequence[TrackedEnvironment]:
        """The tracked environments, from the rows only."""
        ...

    async def untracked_environments(self, tenant_id: UUID) -> int:
        """How many distinct environments the credential rows name that have no tracking row."""
        ...

    async def environment_created_at(
        self, tenant_id: UUID, environment: TrackedEnvironment
    ) -> datetime | None:
        """GitHub's `created_at` for the environment, timezone-aware; None when it is gone."""
        ...

    async def delete_environment(self, tenant_id: UUID, environment: TrackedEnvironment) -> None:
        """Delete the environment from GitHub (absent is fine)."""
        ...

    async def release_environment(self, tenant_id: UUID, environment: TrackedEnvironment) -> None:
        """Stop tracking it: clear its `environment` outputs from the credential rows, then
        forget the row (in that order, so a crash in between repeats harmlessly)."""
        ...


def _gone(
    outputs: Sequence[GithubOutput], environments: Sequence[TrackedEnvironment]
) -> list[dict[str, object]]:
    """One entry per category with a count and the sorted repository ids (ADR 0034 step 3).

    Numeric ids only, never a name; the audit event keeps just category, reason and count.
    """
    return [
        {
            "category": category,
            "reason": GONE_REASON,
            "count": len(items),
            "repository_ids": sorted(
                {i.repository_id for i in items if i.repository_id is not None}
            ),
        }
        for category, items in (
            (SKIPPED_OUTPUTS, outputs),
            (SKIPPED_ENVIRONMENTS, environments),
        )
        if items
    ]


def _untracked(count: int) -> list[dict[str, object]]:
    if not count:
        return []
    return [{"category": KEPT_CATEGORY, "reason": UNTRACKED_REASON, "count": count}]


class GithubDeletionStep:
    """The `GithubStep` over a `GithubPort`."""

    def __init__(self, github: GithubPort) -> None:
        """Remove through `github`."""
        self._github = github

    async def run(self, ctx: StepContext) -> StepOutcome:
        """Remove what can be removed in one pass, or finish once nothing is left."""
        record = ctx.record
        outputs = await self._github.outputs(record.tenant_id)
        environments = await self._github.environments(record.tenant_id)
        untracked = _untracked(await self._github.untracked_environments(record.tenant_id))
        if not outputs and not environments:
            return StepOutcome(skipped=untracked)
        if not await self._github.installation_exists(record.host, record.installation_id):
            return StepOutcome(skipped=[*_gone(outputs, environments), *untracked])
        work: list[GithubOutput | TrackedEnvironment] = [*outputs, *environments]
        # Start where the engine's attempt count points (see the module docstring); the cursor
        # is not touched, because a changed cursor resets the attempts.
        start = record.attempts % len(work)
        counts: Counter[str] = Counter()
        failures: Counter[str] = Counter()
        recreated = 0
        settled = 0
        tries = 0
        spent = False
        for item in [*work[start:], *work[:start]]:
            if tries >= MAX_TRIES_PER_PASS:
                break
            try:
                await ctx.renew()
                tries += 1
                outcome = await self._handle(ctx, item, failures)
            except RunBudgetExhausted:
                spent = True
                break
            if outcome is None:
                continue
            settled += 1
            if outcome == RECREATED_REASON:
                recreated += 1
            else:
                counts[outcome] += 1
        if not settled:
            if spent and not failures:
                raise RunBudgetExhausted
            # Held, also when the budget went on failures: the attempt is counted, which rotates
            # the next start, where releasing the lease would retry the same head items.
            raise _held(failures, len(work))
        skipped = (
            [{"category": KEPT_CATEGORY, "reason": RECREATED_REASON, "count": recreated}]
            if recreated
            else []
        )
        handled = int(record.step_cursor.get("handled", 0)) + settled
        return StepOutcome(
            counts=dict(counts), skipped=skipped, cursor={"handled": handled}, done=False
        )

    async def _handle(
        self, ctx: StepContext, item: GithubOutput | TrackedEnvironment, failures: Counter[str]
    ) -> str | None:
        """Settle one item: the count category, `recreated`, or None when it failed."""
        record = ctx.record
        try:
            if isinstance(item, GithubOutput):
                await self._github.remove_output(record.tenant_id, item)
                return COUNT_OUTPUTS
            return await self._environment(record.tenant_id, item)
        except LeaseLost, RunBudgetExhausted, NonRetryableError:
            raise
        except Exception as error:
            failures[type(error).__name__] += 1
            logger.warning(
                "tenant deletion %s: github removal failed (%s), skipping it this pass",
                record.id,
                type(error).__name__,
            )
            return None

    async def _environment(self, tenant_id: UUID, environment: TrackedEnvironment) -> str:
        """Delete a tracked environment unless it was recreated; always stop tracking it."""
        created = await self._github.environment_created_at(tenant_id, environment)
        if created is None:
            outcome = COUNT_ENVIRONMENTS  # already gone: done
        elif created > environment.created_at + CREATED_AT_SKEW:
            outcome = RECREATED_REASON  # someone else's environment of the same name: kept
        else:
            await self._github.delete_environment(tenant_id, environment)
            outcome = COUNT_ENVIRONMENTS
        await self._github.release_environment(tenant_id, environment)
        return outcome


def _held(failures: Counter[str], total: int) -> GithubStepHeldError:
    kinds = ", ".join(f"{name} x{n}" for name, n in sorted(failures.items()))
    return GithubStepHeldError(f"{sum(failures.values())} of {total} items failed ({kinds})")
