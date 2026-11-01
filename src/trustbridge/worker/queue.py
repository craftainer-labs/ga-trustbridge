"""The Valkey stream client (redis-py `redis.asyncio`, ADR 0013 and 0028 "Queue client").

The stream `tb:events` carries delivery ids only; Postgres is the source of truth. The
dead-letter stream `tb:events:dead` carries `{delivery_id, error}` and no body. Everything
here is the shared Redis protocol subset Valkey implements, kept behind this class so a
client switch stays contained.

Trimming (invariant 6) removes only entries strictly below the smallest of every group's
oldest pending entry and last-delivered id, so nothing unread or unacknowledged is lost,
including when nothing is pending and when the group is stalled (last-delivered `0-0`).
"""

import logging
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError

logger = logging.getLogger(__name__)

EVENTS_STREAM = "tb:events"
DEAD_STREAM = "tb:events:dead"
GROUP = "tb-workers"
_ZERO_ID = "0-0"


@dataclass(frozen=True, slots=True)
class StreamEntry:
    """One stream entry read by a consumer."""

    entry_id: str
    delivery_id: str


def _text(value: object) -> str:
    """Decode bytes to text (a client built without `decode_responses`) or pass str through."""
    return value.decode() if isinstance(value, bytes) else str(value)


def _id_key(entry_id: str) -> tuple[int, int]:
    """Sortable form of a stream id (`ms-seq`)."""
    ms, _, seq = entry_id.partition("-")
    return int(ms), int(seq or 0)


def _entries(raw: Any) -> list[StreamEntry]:  # noqa: ANN401 -- redis-py reply shapes
    """Normalise the reply of XREADGROUP (RESP2 list or RESP3 dict) / XAUTOCLAIM items."""
    items: list[tuple[Any, Any]] = []
    if isinstance(raw, dict):
        for stream_entries in raw.values():
            for chunk in stream_entries:
                items.extend(chunk if isinstance(chunk, list) else [chunk])
    else:
        for _name, stream_entries in raw or []:
            items.extend(stream_entries)
    found = []
    for entry_id, fields in items:
        if not fields:
            continue
        mapping = {_text(k): _text(v) for k, v in fields.items()}
        if "id" in mapping:
            found.append(StreamEntry(_text(entry_id), mapping["id"]))
    return found


class StreamQueue:
    """Producer and consumer-group operations on `tb:events`."""

    def __init__(
        self,
        client: Redis,
        *,
        stream: str = EVENTS_STREAM,
        dead_stream: str = DEAD_STREAM,
        group: str = GROUP,
    ) -> None:
        """Bind a Redis client (`decode_responses=True` preferred) and the stream names."""
        self._r = client
        self.stream = stream
        self.dead_stream = dead_stream
        self.group = group

    async def ensure_group(self) -> None:
        """Create the consumer group (and the stream) if absent; idempotent."""
        try:
            await self._r.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def add(self, delivery_id: str) -> str:
        """Add a delivery id; the same id may be added again (processing is idempotent)."""
        return _text(await self._r.xadd(self.stream, {"id": delivery_id}))

    async def read(self, consumer: str, *, count: int, block_ms: int) -> list[StreamEntry]:
        """Read new entries for this consumer (each is now pending for it)."""
        raw = await self._r.xreadgroup(
            self.group, consumer, {self.stream: ">"}, count=count, block=block_ms
        )
        return _entries(raw)

    async def claim_stale(
        self, consumer: str, *, min_idle_ms: int, count: int
    ) -> list[StreamEntry]:
        """Claim pending entries idle for `min_idle_ms` (a dead consumer's); counts a delivery."""
        raw = await self._r.xautoclaim(
            self.stream, self.group, consumer, min_idle_time=min_idle_ms, start_id="0-0", count=count
        )
        items = raw[1] if len(raw) > 1 else []
        return _entries([(self.stream, items)])

    async def delivery_count(self, entry_id: str) -> int:
        """How many times the stream has delivered `entry_id` (1 for a fresh read)."""
        rows = await self._r.xpending_range(self.stream, self.group, entry_id, entry_id, 1)
        return int(rows[0]["times_delivered"]) if rows else 0

    async def ack(self, entry_id: str) -> None:
        """Acknowledge an entry (it leaves the pending list)."""
        await self._r.xack(self.stream, self.group, entry_id)

    async def dead_letter(self, delivery_id: str, error: str) -> None:
        """Add `{delivery_id, error}` to the dead-letter stream."""
        await self._r.xadd(self.dead_stream, {"delivery_id": delivery_id, "error": error[:1000]})

    async def group_infos(self) -> list[dict[str, Any]]:
        """XINFO GROUPS as dicts with text keys (empty when the stream or group is missing)."""
        try:
            raw = await self._r.xinfo_groups(self.stream)
        except ResponseError:
            return []
        return [{_text(k): v for k, v in info.items()} for info in raw]

    async def trim_events(self) -> int:
        """Trim `tb:events` below what every group has delivered and acknowledged.

        With no group, nothing is trimmed. The boundary is the minimum, over groups, of the
        oldest pending id and the last-delivered id; `XTRIM MINID` removes strictly smaller
        ids, so the pending entry itself and everything unread are kept.
        """
        bounds: list[str] = []
        for info in await self.group_infos():
            last = _text(info.get("last-delivered-id", _ZERO_ID))
            candidates = [last]
            if int(info.get("pending", 0)):
                pending = await self._r.xpending(self.stream, _text(info["name"]))
                low = pending.get("min") if isinstance(pending, dict) else None
                if low is not None:
                    candidates.append(_text(low))
            bounds.append(min(candidates, key=_id_key))
        if not bounds:
            return 0
        boundary = min(bounds, key=_id_key)
        if _id_key(boundary) == (0, 0):
            return 0
        return int(await self._r.xtrim(self.stream, minid=boundary, approximate=False))

    async def trim_dead(self, older_than_ms: int) -> int:
        """Trim dead-letter entries whose id timestamp is older than `older_than_ms` (epoch)."""
        if older_than_ms <= 0:
            return 0
        return int(await self._r.xtrim(self.dead_stream, minid=f"{older_than_ms}-0", approximate=False))

    async def lag(self) -> int | None:
        """Group lag (entries not yet delivered); None when the server does not report it."""
        lags = [info.get("lag") for info in await self.group_infos()]
        known = [int(v) for v in lags if v is not None]
        return max(known) if known else None
