"""Bounding the `authz.denied` rows (ADR 0032 "Events", user decision A6 of NG6d).

A principal with a valid token can ask for random tenant ids and be denied each time, so an
unbounded sink would let one token fill the audit table. `ThrottledDenials` wraps a `DenialSink`:
per (subject, reason) and fixed window of `window_seconds`, the first `burst` denials are
recorded in full; the rest are only counted (in the shared counter store, and in the
`suppressed` metric). When the window has closed, ONE summary row (a `Denial` with
`suppressed > 0`) records how many were not written.

**Nothing disappears from the trail.** A window that suppressed anything is entered in a shared
*pending index*: the set `tb:audit:denied:pending` of subject/reason hashes, and per hash the set
`tb:audit:denied:pending:<hash>` of window numbers, together with the last suppressed denial as
the window's sample (`...:sample`). All of it lives in the counter store, so no process-local
state is needed: (a) the first denial of the same (subject, reason) in a later
window summarises EVERY closed pending window of that pair, however many windows ago, and (b)
`flush()` (the api calls it every window, at startup and at shutdown) summarises every closed
pending window of every subject, on any replica, also after a restart. A claim marker (`INCR` to
1 per window) makes each window's summary happen once across replicas and paths. If writing the
row fails (or the task is cancelled mid-write) the claim is released again, so the window is
retried; the window leaves the index only after its row is written. Keys are kept
`max(2 * window, 1 day)`, so an outage of the whole api shorter than that loses nothing. What
remains is a process killed between claiming and writing, and a denial that was both counted and
recorded because noting it failed (the summary then counts it once more).

A counter-store failure or timeout fails open to *recording the row*: the audit trail stays
complete, and the request path stays bounded by `timeout_ms`.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, replace

from trustbridge.audit.denial import Denial, DenialSink
from trustbridge.ratelimit import CounterLike, WindowCounter

logger = logging.getLogger(__name__)
KEY_PREFIX = "tb:audit:denied"
PENDING_KEY = f"{KEY_PREFIX}:pending"
MIN_RETENTION_SECONDS = 24 * 3600
"""How long counts, samples and the pending set are kept (at least two windows)."""


type _Claimed = tuple[str, int, Denial]
"""A summary this process claimed: the subject/reason hash, the window and the row to write."""


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
        self._retain = max(2 * window_seconds, MIN_RETENTION_SECONDS)

    @staticmethod
    def _base(denial: Denial) -> str:
        """The bounded, fixed-length key part for a (subject, reason) pair."""
        digest = hashlib.sha256(f"{denial.sub}\0{denial.reason}".encode()).hexdigest()
        return digest[:32]

    @staticmethod
    def _key(base: str, window: int) -> str:
        return f"{KEY_PREFIX}:{base}:{window}"

    def _current(self) -> int:
        return int(self._clock() // self._window)

    async def denied(self, denial: Denial) -> None:
        """Record the denial, or count it when this (subject, reason) is over its burst."""
        window = self._current()
        base = self._base(denial)
        count = 0  # 0: the counter store failed, so the row is recorded
        claimed: list[_Claimed] = []
        try:
            async with asyncio.timeout(self._timeout):
                count = await self._counter.incr(self._key(base, window), self._retain)
                if count == 1:
                    await self._claim_closed(base, window, claimed)
                if count > self._burst and not await self._note_suppressed(base, window, denial):
                    count = 0  # not noted for a summary, so the row itself is the record
        except Exception:
            logger.warning("denial throttle failed open", exc_info=True)
            if count > self._burst:
                count = 0
        for item in claimed:  # whatever was claimed before a failure is still written
            await self._deliver(*item)
        if count > self._burst:
            if self._suppressed is not None:
                self._suppressed.add(1)
            return
        await self._sink.denied(denial)

    async def _note_suppressed(self, base: str, window: int, denial: Denial) -> bool:
        """Keep the latest suppressed denial as the window's sample and mark the window pending.

        Written on every suppressed denial (`SADD` is idempotent), so a failure on the first one
        leaves no window unnoted. False when the window was summarised already (a request that
        straddled the window end): the caller records that denial itself.
        """
        if await self._counter.get(self._claim_key(base, window)) > 0:
            return False
        sample = json.dumps(asdict(replace(denial, suppressed=0, window_seconds=0)))
        await self._counter.set_text(f"{self._key(base, window)}:sample", sample, self._retain)
        await self._counter.sadd(self._index(base), str(window), self._retain)
        await self._counter.sadd(PENDING_KEY, base, self._retain)
        return True

    def _claim_key(self, base: str, window: int) -> str:
        return f"{self._key(base, window)}:summary"

    @staticmethod
    def _index(base: str) -> str:
        return f"{PENDING_KEY}:{base}"

    @staticmethod
    def _closed(pending: set[str], current: int) -> list[int]:
        """The window numbers in `pending` that have closed, oldest first."""
        return sorted(int(w) for w in pending if w.isdigit() and int(w) < current)

    async def _claim_closed(self, base: str, current: int, out: list[_Claimed]) -> None:
        """Claim the summary of every closed pending window of `base`, appending each to `out`."""
        for window in self._closed(await self._counter.smembers(self._index(base)), current):
            summary = await self._prepare(base, window)
            if summary is not None:
                out.append((base, window, summary))

    async def _prepare(self, base: str, window: int) -> Denial | None:
        """Claim the summary of a closed `window` if it suppressed anything and is unclaimed.

        Everything that can fail is read BEFORE the claim, so a failure leaves the window
        pending for the next pass.
        """
        key = self._key(base, window)
        suppressed = await self._counter.get(key) - self._burst
        text = await self._counter.get_text(f"{key}:sample") if suppressed > 0 else None
        if text is None:
            if suppressed > 0:
                logger.error("denial summary lost its sample", extra={"window": window})
            await self._forget(base, window)
            return None
        if await self._counter.incr(self._claim_key(base, window), self._retain) != 1:
            await self._forget(base, window)  # another replica or path writes it
            return None
        return replace(
            Denial(**json.loads(text)), suppressed=suppressed, window_seconds=self._window
        )

    async def _forget(self, base: str, window: int) -> None:
        """Take `window` out of the pending index (and `base` when it was its last window)."""
        await self._counter.srem(self._index(base), str(window))
        if not await self._counter.smembers(self._index(base)):
            await self._counter.srem(PENDING_KEY, base)

    async def _deliver(self, base: str, window: int, summary: Denial) -> None:
        """Write a claimed summary; release the claim if that fails so the window is retried."""
        try:
            await self._sink.denied(summary)
        except BaseException:
            with contextlib.suppress(Exception):
                await asyncio.shield(self._counter.delete(self._claim_key(base, window)))
            raise
        try:
            async with asyncio.timeout(self._timeout):
                await self._forget(base, window)
        except Exception:
            # The claim is taken, so the next pass finds the entry and just forgets it.
            logger.warning("denial summary written but still indexed", exc_info=True)

    async def flush(self) -> int:
        """Write the summaries of every closed pending window; returns how many were written.

        Driven by the shared index, so any replica (or a restarted one) finishes the work of
        any other. A failure is logged and leaves the entries pending for the next pass.
        """
        current = self._current()
        try:
            async with asyncio.timeout(self._timeout):
                bases = sorted(await self._counter.smembers(PENDING_KEY))
        except Exception:
            logger.warning("denial summary flush failed", exc_info=True)
            return 0
        written = 0
        for base in bases:
            claimed: list[_Claimed] = []
            try:
                async with asyncio.timeout(self._timeout):
                    pending = await self._counter.smembers(self._index(base))
                    if not pending:
                        await self._counter.srem(PENDING_KEY, base)
                    for window in self._closed(pending, current):
                        summary = await self._prepare(base, window)
                        if summary is not None:
                            claimed.append((base, window, summary))
            except Exception:
                logger.warning("denial summary flush failed", exc_info=True)
            for item in claimed:
                await self._deliver(*item)
                written += 1
        return written
