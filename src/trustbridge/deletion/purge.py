"""The retention purge of finished deletion records (ADR 0034 user decision B4, NG6h-3c-2).

`SqlRecordPurger` calls `tb_purge_tenant_deletions` (migration `7b2d4f8a1c36`) in short
transactions, one per batch, and implements `RecordPurger` (`deletion.ports`). The function removes
only `done` records older than the cut-off whose tombstone exists; the tombstone itself
(`tenant_tombstones`) is never touched, so a still-installed App stays refused (B2). Retention 0
never purges. Mirrors the audit purge (`audit.store.purge_expired`): negative retention, a non-zero
retention below the floor, a batch outside 1..10000 and a batch count below 1 fail closed with
`ValueError` before any transaction.
"""

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.deletion import metrics
from trustbridge.deletion.ports import PurgeResult

logger = logging.getLogger(__name__)

MAX_PURGE_BATCH: Final = 10000  # the ceiling `tb_purge_tenant_deletions` enforces
_PURGE = text(
    "SELECT purged, unprotected FROM tb_purge_tenant_deletions("
    "CAST(:before AS timestamptz), CAST(:min_days AS integer), CAST(:batch AS integer))"
)


class SqlRecordPurger:
    """`RecordPurger` over the definer function, run in the app role's own sessions."""

    def __init__(  # noqa: PLR0913 -- every bound is a distinct, keyword-only knob
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        retention_days: int,
        min_days: int,
        batch_size: int,
        max_batches: int,
        budget_seconds: float | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Validate the bounds (fail closed) and keep them; nothing touches the database yet."""
        if retention_days < 0:
            raise ValueError("retention_days must not be negative (0 means never purge)")
        if 0 < retention_days < min_days:
            raise ValueError(f"retention_days must be 0 or at least min_days ({min_days})")
        if min_days < 1:
            raise ValueError("min_days must be at least 1")
        if not 1 <= batch_size <= MAX_PURGE_BATCH:
            raise ValueError(f"batch_size must be between 1 and {MAX_PURGE_BATCH}")
        if max_batches < 1:
            raise ValueError("max_batches must be at least 1")
        self._factory = factory
        self._retention_days = retention_days
        self._min_days = min_days
        self._batch_size = batch_size
        self._max_batches = max_batches
        self._budget_seconds = budget_seconds
        self._clock = clock
        self._monotonic = monotonic

    async def purge(self, on_batch: Callable[[int], None] | None = None) -> PurgeResult:
        """Purge finished records older than the retention, at most `max_batches` batches.

        Stops early when a batch is short, or when `budget_seconds` has elapsed (so a caller
        holding a lease ends before it could lapse); the next call continues. Returns the total
        purged and the unprotected count the last batch reported.
        """
        if self._retention_days == 0:
            return PurgeResult()
        cutoff = self._clock() - timedelta(days=self._retention_days)
        total = unprotected = 0
        started = self._monotonic()
        for _ in range(self._max_batches):
            if self._budget_seconds is not None and (
                self._monotonic() - started > self._budget_seconds
            ):
                break
            async with self._factory() as session, session.begin():
                row = (
                    await session.execute(
                        _PURGE,
                        {
                            "before": cutoff,
                            "min_days": self._min_days,
                            "batch": self._batch_size,
                        },
                    )
                ).one()
            purged, unprotected = int(row.purged), int(row.unprotected)
            total += purged
            if purged:
                metrics.records_purged.add(purged)
                logger.info("deletion record purge batch", extra={"purged_records": purged})
            if on_batch is not None:
                on_batch(purged)
            if purged < self._batch_size:
                break
        return PurgeResult(total, unprotected)
