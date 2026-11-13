"""The draining step: re-scan until two consecutive quiet passes, bounded (ADR 0034)."""

from uuid import UUID

import pytest

from tests.trustbridge.deletion.fakes import TENANT, record
from trustbridge.deletion.drain import DrainNotQuiescent, DrainStep
from trustbridge.deletion.model import Lease, StepContext


class Writers:
    """Returns the scripted active counts, one per scan."""

    def __init__(self, *counts: int) -> None:
        """`counts` is consumed in order; the last repeats."""
        self.counts = list(counts)
        self.seen: list[UUID] = []

    async def quiesce(self, tenant_id: UUID) -> int:
        """The next count."""
        self.seen.append(tenant_id)
        return self.counts.pop(0) if len(self.counts) > 1 else self.counts[0]


def _ctx(renewals: list[int]) -> StepContext:
    rec = record(state="draining")

    async def renew() -> None:
        renewals.append(1)

    return StepContext(rec, Lease(rec.id, "h", 1), renew)


async def _no_sleep(_: float) -> None:
    return None


async def test_two_consecutive_quiet_passes_end_the_drain() -> None:
    """A late writer (a 1 between zeros) resets the quiet count: that is the re-scan."""
    writers = Writers(3, 0, 1, 0, 0)
    renewals: list[int] = []
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    outcome = await DrainStep(writers, poll_seconds=2.5, max_passes=10, sleep=sleep).run(
        _ctx(renewals)
    )
    assert outcome.done is True
    assert len(writers.seen) == 5
    assert set(writers.seen) == {TENANT}
    assert len(renewals) == 5
    assert slept == [2.5] * 4


async def test_a_tenant_that_never_quiesces_raises_a_transient_error() -> None:
    """Bounded passes: the engine retries the step with backoff, never advances."""
    writers = Writers(1)
    with pytest.raises(DrainNotQuiescent, match="3 passes"):
        await DrainStep(writers, poll_seconds=0, max_passes=3, sleep=_no_sleep).run(_ctx([]))
    assert len(writers.seen) == 3


@pytest.mark.parametrize(("poll", "passes"), [(-1, 5), (1, 1)])
def test_the_parameters_are_validated(poll: float, passes: int) -> None:
    """A negative poll or fewer passes than the quiet run makes no sense."""
    with pytest.raises(ValueError, match="max_passes"):
        DrainStep(Writers(0), poll_seconds=poll, max_passes=passes)


async def test_the_default_sleep_is_asyncio_sleep() -> None:
    """With the default `sleep`, a zero poll still completes."""
    outcome = await DrainStep(Writers(0), poll_seconds=0, max_passes=2).run(_ctx([]))
    assert outcome.done is True
