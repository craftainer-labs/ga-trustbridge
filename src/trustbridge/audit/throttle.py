"""Bounding the `authz.denied` rows (ADR 0032 "Events", user decision A6 of NG6d).

A principal with a valid token can ask for random tenant ids and be denied each time, so an
unbounded sink would let one token fill the audit table. `ThrottledDenials` wraps a `DenialSink`:
per (subject, reason) and fixed window of `window_seconds`, the first `burst` denials are
recorded in full; the rest are only counted (in the shared counter store, and in the
`suppressed` metric). When the window has closed, ONE summary row (a `Denial` with
`suppressed > 0`) records how many were not written.

**When the summary is written.** Either (a) on the first denial of the same (subject, reason) in
a later window, or (b) by `flush()`, which the api's lifespan calls every window. The counts live
in the shared counter store (Valkey), so a replica restart does not lose them; what a restart
loses is only the process-local index of windows `flush()` walks, so a summary is then written by
path (a) when that subject is denied again, and not at all if it never is. A claim marker
(`INCR` to 1) makes the summary happen once across replicas and paths.

A counter-store failure or timeout fails open to *recording the row*: the audit trail stays
complete, and the request path stays bounded by `timeout_ms`.
"""

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import replace

from trustbridge.audit.denial import Denial, DenialSink
from trustbridge.ratelimit import CounterLike, WindowCounter

logger = logging.getLogger(__name__)
KEY_PREFIX = "tb:audit:denied"
MAX_OPEN_WINDOWS = 10_000
"""Most (subject, reason) windows the process remembers for `flush()`; beyond it only path (a)."""


class ThrottledDenials:
    """A `DenialSink` that records the first `burst` denials per (subject, reason) and window."""

    def __init__(  # noqa: PLR0913 - the numbers are all settings
        self,
        sink: DenialSink,
        counter: WindowCounter,
        *,
        window_seconds: int,
        burst: int,
        timeout_ms: int,
        suppressed: CounterLike | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Wrap `sink`; `counter` is shared by every replica."""
        self._sink = sink
        self._counter = counter
        self._window = window_seconds
        self._burst = burst
        self._timeout = timeout_ms / 1000
        self._suppressed = suppressed
        self._clock = clock
        self._open: dict[str, tuple[int, Denial]] = {}

    @staticmethod
    def _base(denial: Denial) -> str:
        """The bounded, fixed-length key part for a (subject, reason) pair."""
        digest = hashlib.sha256(f"{denial.sub}\0{denial.reason}".encode()).hexdigest()
        return digest[:32]

    def _key(self, base: str, window: int) -> str:
        return f"{KEY_PREFIX}:{base}:{window}"

    async def denied(self, denial: Denial) -> None:
        """Record the denial, or count it when this (subject, reason) is over its burst."""
        window = int(self._clock() // self._window)
        base = self._base(denial)
        count = 0  # 0: the counter store failed, so the row is recorded
        summary: Denial | None = None
        try:
            async with asyncio.timeout(self._timeout):
                count = await self._counter.incr(self._key(base, window), 2 * self._window)
                if count == 1:
                    summary = await self._claim_summary(base, window - 1, denial)
        except Exception:
            logger.warning("denial throttle failed open", exc_info=True)
        if summary is not None:
            await self._sink.denied(summary)
        if count > self._burst:
            if self._suppressed is not None:
                self._suppressed.add(1)
            if base in self._open or len(self._open) < MAX_OPEN_WINDOWS:
                self._open[base] = (window, denial)
            return
        await self._sink.denied(denial)

    async def _claim_summary(self, base: str, window: int, sample: Denial) -> Denial | None:
        """The summary of `window` if it had suppressed denials and nobody claimed it yet."""
        suppressed = await self._counter.get(self._key(base, window)) - self._burst
        if suppressed <= 0:
            self._open.pop(base, None)
            return None
        claim = await self._counter.incr(f"{self._key(base, window)}:summary", 2 * self._window)
        known = self._open.pop(base, None)
        if claim != 1:
            return None
        return replace(
            known[1] if known is not None else sample,
            suppressed=suppressed,
            window_seconds=self._window,
        )

    async def flush(self) -> int:
        """Write the summaries of windows that have closed; returns how many were written.

        Call it about once a window. A failure for one entry is logged and the entry dropped
        (the count stays in the store for path (a)).
        """
        current = int(self._clock() // self._window)
        written = 0
        for base, (window, sample) in list(self._open.items()):
            if window >= current:
                continue
            del self._open[base]
            try:
                async with asyncio.timeout(self._timeout):
                    suppressed = await self._counter.get(self._key(base, window)) - self._burst
                    claim = (
                        await self._counter.incr(
                            f"{self._key(base, window)}:summary", 2 * self._window
                        )
                        if suppressed > 0
                        else 0
                    )
            except Exception:
                logger.warning("denial summary flush failed", exc_info=True)
                continue
            if claim == 1:
                await self._sink.denied(
                    replace(sample, suppressed=suppressed, window_seconds=self._window)
                )
                written += 1
        return written
