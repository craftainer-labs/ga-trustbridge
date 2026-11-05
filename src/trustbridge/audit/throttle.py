"""Bounding the `authz.denied` rows (ADR 0032 "Events", user decision A6 of NG6d).

A principal with a valid token can ask for random tenant ids and be denied each time, so an
unbounded sink would let one token fill the audit table. `ThrottledDenials` wraps a `DenialSink`:
per (subject, reason) and fixed window of `window_seconds`, the first `burst` denials are
recorded in full; the rest are only counted (in the shared counter store, and in the
`suppressed` metric). When the window has closed, ONE summary row (a `Denial` with
`suppressed > 0`) records how many were not written.

**Nothing disappears from the trail.** A window that suppressed anything is entered in a shared
*pending set* (`tb:audit:denied:pending`, members `<subject/reason hash>:<window>`) together with
the last suppressed denial as its sample (`...:sample`). Both live in the counter store, so no
process-local state is needed: (a) the first denial of the same (subject, reason) in a later
window summarises EVERY closed pending window of that pair, however many windows ago, and (b)
`flush()` (the api calls it every window, at startup and at shutdown) summarises every closed
pending window of every subject, on any replica, also after a restart. A claim marker (`INCR` to
1 per window) makes each window's summary happen once across replicas and paths. Keys are kept
`max(2 * window, 1 day)`, so an outage of the whole api shorter than that loses nothing.

A counter-store failure or timeout fails open to *recording the row*: the audit trail stays
complete, and the request path stays bounded by `timeout_ms`.
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

    def _current(self) -> int:
        return int(self._clock() // self._window)

    async def denied(self, denial: Denial) -> None:
        """Record the denial, or count it when this (subject, reason) is over its burst."""
        window = self._current()
        base = self._base(denial)
        count = 0  # 0: the counter store failed, so the row is recorded
        summaries: list[Denial] = []
        try:
            async with asyncio.timeout(self._timeout):
                count = await self._counter.incr(self._key(base, window), self._retain)
                if count == 1:
                    summaries = await self._claim_closed(base, window)
                if count > self._burst:
                    await self._note_suppressed(base, window, denial)
        except Exception:
            logger.warning("denial throttle failed open", exc_info=True)
            if count > self._burst:
                count = 0  # it could not be noted for a summary, so record the row itself
        for summary in summaries:
            await self._sink.denied(summary)
        if count > self._burst:
            if self._suppressed is not None:
                self._suppressed.add(1)
            return
        await self._sink.denied(denial)

    async def _note_suppressed(self, base: str, window: int, denial: Denial) -> None:
        """Keep the latest suppressed denial as the window's sample; enter the window as pending.

        The pending entry is written on every suppressed denial (`SADD` is idempotent), so a
        failure on the first one does not leave the window unnoted.
        """
        sample = json.dumps(asdict(replace(denial, suppressed=0, window_seconds=0)))
        await self._counter.set_text(f"{self._key(base, window)}:sample", sample, self._retain)
        await self._counter.sadd(PENDING_KEY, f"{base}:{window}", self._retain)

    async def _claim_closed(self, base: str, current: int) -> list[Denial]:
        """The summaries of every closed pending window of `base` this call claimed."""
        out: list[Denial] = []
        for member in sorted(await self._counter.smembers(PENDING_KEY)):
            member_base, _, text = member.partition(":")
            if member_base != base or not text.isdigit() or int(text) >= current:
                continue
            summary = await self._summarise(member_base, int(text))
            if summary is not None:
                out.append(summary)
        return out

    async def _summarise(self, base: str, window: int) -> Denial | None:
        """The summary of a closed `window`, if it suppressed anything and nobody claimed it."""
        key = self._key(base, window)
        member = f"{base}:{window}"
        suppressed = await self._counter.get(key) - self._burst
        if suppressed <= 0:
            await self._counter.srem(PENDING_KEY, member)
            return None
        claim = await self._counter.incr(f"{key}:summary", self._retain)
        await self._counter.srem(PENDING_KEY, member)
        if claim != 1:
            return None
        text = await self._counter.get_text(f"{key}:sample")
        if text is None:
            logger.error("denial summary lost its sample", extra={"window": window})
            return None
        return replace(
            Denial(**json.loads(text)), suppressed=suppressed, window_seconds=self._window
        )

    async def flush(self) -> int:
        """Write the summaries of every closed pending window; returns how many were written.

        Driven by the shared pending set, so any replica (or a restarted one) finishes the
        work of any other. A failure for one entry is logged; the entry stays pending for the
        next pass unless it was already claimed.
        """
        current = self._current()
        try:
            async with asyncio.timeout(self._timeout):
                members = sorted(await self._counter.smembers(PENDING_KEY))
        except Exception:
            logger.warning("denial summary flush failed", exc_info=True)
            return 0
        written = 0
        for member in members:
            base, _, text = member.partition(":")
            if not text.isdigit() or int(text) >= current:
                continue
            try:
                async with asyncio.timeout(self._timeout):
                    summary = await self._summarise(base, int(text))
            except Exception:
                logger.warning("denial summary flush failed", exc_info=True)
                continue
            if summary is not None:
                await self._sink.denied(summary)
                written += 1
        return written
