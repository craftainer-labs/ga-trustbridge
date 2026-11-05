"""Bounding the `authz.denied` rows (ADR 0032 "Events", user decision A6 of NG6d).

A principal with a valid token can ask for random tenant ids and be denied each time, so an
unbounded sink would let one token fill the audit table. `ThrottledDenials` wraps a `DenialSink`:
per (subject, reason) and fixed window of `window_seconds`, the first `burst` denials are
recorded in full; the rest are only counted (in the shared counter store, and in the
`suppressed` metric). After the window has closed, a summary row (a `Denial` with
`suppressed > 0` and the `window` number) records how many were not written.

**Delivered at least once, never lost.** Every suppressed denial notes its window in a shared
*pending index* (the set `tb:audit:denied:pending` of subject/reason hashes, and per hash the set
`tb:audit:denied:pending:<hash>` of window numbers) and stores itself as the window's sample
(`...:sample`), in one call. All of it lives in the counter store, so no process-local state is
needed. A summary is written by (a) the first denial of the same (subject, reason) in a later
window, for EVERY closed pending window of that pair, and (b) `flush()` (the api calls it at
startup, every window and at shutdown), for every closed pending window of every subject, on any
replica and after a restart. There is NO claim before the write: a replica reads the window,
writes the row, and only then marks the window done (the count it summarised) and removes it
from the index. Two replicas that race write the same row twice (equal `sub`, `reason` and
`window`); a crash, a timeout, a cancel or a failing sink at any point leaves the window pending,
so the next pass writes it. A denial counted after a summary was written (a request that
straddled the window end) makes the window pending again and gets its own, smaller, summary.
Keys are kept `max(2 * window, 1 day)`.

Failure isolation: the current denial is always recorded first; old summaries follow, each in its
own try/except, so an old summary can never fail a request. A counter-store failure or timeout
while counting or noting fails open to *recording the row*, so the audit trail stays complete and
the request path stays bounded by `timeout_ms`.
"""

import asyncio
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

    @staticmethod
    def _index(base: str) -> str:
        return f"{PENDING_KEY}:{base}"

    @staticmethod
    def _closed(pending: set[str], current: int) -> list[int]:
        """The window numbers in `pending` that have closed, oldest first."""
        return sorted(int(w) for w in pending if w.isdigit() and int(w) < current)

    def _current(self) -> int:
        return int(self._clock() // self._window)

    async def denied(self, denial: Denial) -> None:
        """Record the denial, or count it when this (subject, reason) is over its burst.

        The current denial comes first; summaries of earlier windows follow and cannot fail it.
        """
        window = self._current()
        base = self._base(denial)
        count = 0  # 0: the counter store failed, so the row is recorded
        old: list[int] = []
        try:
            async with asyncio.timeout(self._timeout):
                count = await self._counter.incr(self._key(base, window), self._retain)
                if count == 1:
                    old = self._closed(await self._counter.smembers(self._index(base)), window)
                if count > self._burst:
                    await self._note(base, window, denial)
        except Exception:
            logger.warning("denial throttle failed open", exc_info=True)
            if count > self._burst:
                count = 0  # it could not be noted for a summary, so the row itself is the record
        if count > self._burst:
            if self._suppressed is not None:
                self._suppressed.add(1)
        else:
            await self._sink.denied(denial)
        for closed in old:
            await self._summarise(base, closed)

    async def _note(self, base: str, window: int, denial: Denial) -> None:
        """Make the window pending and keep this denial as its sample (one call)."""
        sample = json.dumps(asdict(replace(denial, suppressed=0, window_seconds=0, window=0)))
        await self._counter.note(
            f"{self._key(base, window)}:sample",
            sample,
            {self._index(base): str(window), PENDING_KEY: base},
            self._retain,
        )

    async def _summarise(self, base: str, window: int) -> bool:
        """Write the summary of a closed `window` if it has one; True when a row was written.

        Never raises (cancellation excepted): a failure is logged and the window stays pending.
        """
        key = self._key(base, window)
        try:
            async with asyncio.timeout(self._timeout):
                total = await self._counter.get(key) - self._burst
                done = int(await self._counter.get_text(f"{key}:done") or 0)
                text = await self._counter.get_text(f"{key}:sample") if total > done else None
                if text is None:
                    if total > done:
                        logger.error("denial summary lost its sample", extra={"window": window})
                    await self._counter.srem(self._index(base), str(window))
                    return False
            summary = replace(
                Denial(**json.loads(text)),
                suppressed=total - done,
                window_seconds=self._window,
                window=window,
            )
            await self._sink.denied(summary)  # the row first; marking it done comes after
            async with asyncio.timeout(self._timeout):
                await self._complete(base, window, total)
        except Exception:
            logger.exception("denial summary not completed; it stays pending")
            return False
        return True

    async def _complete(self, base: str, window: int, total: int) -> None:
        """Mark `total` suppressed denials of the window as summarised and drop it from the index.

        A denial counted meanwhile (a request that straddled the window end) puts it back.
        """
        key = self._key(base, window)
        await self._counter.set_text(f"{key}:done", str(total), self._retain)
        await self._counter.srem(self._index(base), str(window))
        if await self._counter.get(key) - self._burst > total:
            await self._counter.sadd(self._index(base), str(window), self._retain)

    async def flush(self) -> int:
        """Write the summaries of every closed pending window; returns how many were written.

        Driven by the shared index, so any replica (or a restarted one) finishes the work of
        any other, and a flush cut short (shutdown) is simply continued by the next one. One
        entry's failure is logged and does not stop the others. Bases are not removed from the
        index eagerly; the key expires.
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
            try:
                async with asyncio.timeout(self._timeout):
                    windows = self._closed(await self._counter.smembers(self._index(base)), current)
            except Exception:
                logger.warning("denial summary flush failed", exc_info=True)
                continue
            for window in windows:
                written += await self._summarise(base, window)
        return written
