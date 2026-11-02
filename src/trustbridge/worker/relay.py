"""The relay: adds delivery ids (only) from the outbox to the stream (ADR 0028 item 2).

Used three ways: best-effort from the ingress after the response (`relay_one`), by the
reconciler's periodic tick (never relayed or due retries) and re-relay sweep (everything
pending, after stream loss), and by a worker when a host becomes `ready` again (that host's
waiting rows). Adding an id twice is harmless; handlers are idempotent.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from trustbridge.worker.deliveries import DeliveryStore, RelayMode
from trustbridge.worker.queue import StreamQueue

logger = logging.getLogger(__name__)
Clock = Callable[[], datetime]


def utcnow() -> datetime:
    """The current time, timezone-aware UTC."""
    return datetime.now(UTC)


class Relay:
    """Moves ids from `webhook_deliveries` to `tb:events`."""

    def __init__(
        self,
        store: DeliveryStore,
        queue: StreamQueue,
        *,
        batch_size: int = 5000,
        clock: Clock = utcnow,
    ) -> None:
        """Bind the outbox, the stream and the batch size."""
        self._store = store
        self._queue = queue
        self._batch = batch_size
        self._clock = clock

    async def relay_ids(self, delivery_ids: list[str]) -> int:
        """Add ids to the stream and record the relay; a stream failure stops the batch."""
        relayed: list[str] = []
        try:
            for delivery_id in delivery_ids:
                await self._queue.add(delivery_id)
                relayed.append(delivery_id)
        finally:
            await self._store.mark_relayed(relayed, self._clock())
        return len(relayed)

    async def relay_one(self, delivery_id: str) -> None:
        """Best-effort relay after the ingress response; never raises."""
        try:
            await self.relay_ids([delivery_id])
        except Exception:
            logger.warning("best-effort relay failed; the reconciler will relay it", exc_info=True)

    async def run_mode(
        self, mode: RelayMode, *, host: str | None = None, min_age_seconds: float = 0.0
    ) -> int:
        """Relay the ids the mode selects (at most `batch_size` per call); returns the count.

        One query per call: relaying stamps `last_relayed_at`, so looping on the same
        selection could re-select rows. Rows beyond the cap are picked up on the next call.
        """
        now = self._clock()
        ids = await self._store.list_relayable(
            now,
            mode=mode,
            host=host,
            resweep_before=now - timedelta(seconds=min_age_seconds),
            limit=self._batch,
        )
        return await self.relay_ids(ids) if ids else 0

    async def tick(self) -> int:
        """Relay rows never relayed and delayed retries that are now due."""
        return await self.run_mode("tick")

    async def resweep(self, min_age_seconds: float) -> int:
        """Re-add every pending row not relayed within `min_age_seconds` (stream-loss repair)."""
        return await self.run_mode("resweep", min_age_seconds=min_age_seconds)

    async def relay_host(self, host: str) -> int:
        """Re-add the pending rows of `host` (it just became ready)."""
        return await self.run_mode("host", host=host)
