"""The draining step: re-scan the tenant's writers until two quiet passes in a row (ADR 0034).

There is no writer-barrier column (NG6h-4a ruling 3): an admin-disabled tenant's handlers are
skipped, the `deleting` refusals stop new writes, and this scan catches what was recorded after
the first look before the database step runs.
"""

import asyncio
from collections.abc import Awaitable, Callable

from trustbridge.deletion.model import StepContext, StepOutcome
from trustbridge.deletion.steps import WritersPort

QUIET_PASSES = 2
"""Consecutive zero passes that make the drain quiescent."""


class DrainNotQuiescent(Exception):  # noqa: N818 -- a transient condition: the step retries
    """The tenant still had active writers after the pass limit: retried with backoff."""


class DrainStep:
    """The `draining` step over a `WritersPort`."""

    def __init__(
        self,
        writers: WritersPort,
        *,
        poll_seconds: float,
        max_passes: int,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Poll every `poll_seconds`, at most `max_passes` passes (`sleep` is injectable)."""
        if poll_seconds < 0 or max_passes < QUIET_PASSES:
            raise ValueError("poll_seconds must not be negative; max_passes must allow two passes")
        self._writers = writers
        self._poll = poll_seconds
        self._max_passes = max_passes
        self._sleep = sleep

    async def run(self, ctx: StepContext) -> StepOutcome:
        """Scan until `QUIET_PASSES` consecutive passes find nothing, renewing between passes."""
        quiet = 0
        for _ in range(self._max_passes):
            await ctx.renew()
            active = await self._writers.quiesce(ctx.record.tenant_id)
            quiet = quiet + 1 if active == 0 else 0
            if quiet >= QUIET_PASSES:
                return StepOutcome()
            await self._sleep(self._poll)
        raise DrainNotQuiescent(f"writers still active after {self._max_passes} passes")
