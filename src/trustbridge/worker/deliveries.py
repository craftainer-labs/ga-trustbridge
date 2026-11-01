"""The delivery outbox: records, the `DeliveryStore` Protocol, Postgres and in-memory stores.

`webhook_deliveries` is the source of truth (ADR 0028 "Request path and processing"). The
Postgres store commits each write in its own transaction before returning, so ingress can
answer 2xx only after the row is durable. The in-memory store backs `MODE=mock` and unit
tests; the Postgres store is covered by the integration tests.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Literal, Protocol

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.worker.models import WebhookDelivery

RelayMode = Literal["tick", "resweep", "host"]
PENDING = "pending"
PROCESSED = "processed"
DEAD = "dead"


@dataclass(frozen=True, slots=True)
class NewDelivery:
    """What ingress stores: id, host, event, allow-listed headers and the body."""

    delivery_id: str
    host: str
    event: str
    headers: dict[str, str]
    body: bytes
    received_at: datetime


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    """A stored delivery row."""

    delivery_id: str
    host: str
    event: str
    headers: dict[str, str]
    body: bytes | None
    received_at: datetime
    status: str = PENDING
    attempts: int = 0
    next_attempt_at: datetime | None = None
    last_relayed_at: datetime | None = None
    processed_at: datetime | None = None
    dead_at: datetime | None = None
    last_error: str | None = None


class DeliveryStore(Protocol):
    """Persistence of the outbox; every method commits before it returns."""

    async def insert(self, delivery: NewDelivery) -> bool:
        """Store a delivery; False (and no second row) if its id already exists."""
        ...

    async def get(self, delivery_id: str) -> DeliveryRecord | None:
        """The row for `delivery_id`, or None."""
        ...

    async def existing_ids(self, delivery_ids: Sequence[str]) -> set[str]:
        """The subset of `delivery_ids` that has a stored row."""
        ...

    async def mark_processed(self, delivery_id: str, now: datetime) -> None:
        """Mark a pending row processed."""
        ...

    async def record_failure(
        self, delivery_id: str, error: str, now: datetime, next_attempt_at: datetime
    ) -> int:
        """Count one handler failure and schedule the retry; returns the new counter."""
        ...

    async def defer(self, delivery_id: str, until: datetime) -> None:
        """Delay a row without counting a failure (busy lease)."""
        ...

    async def mark_dead(self, delivery_id: str, error: str, now: datetime) -> None:
        """Move a row to `dead`."""
        ...

    async def requeue(self, delivery_id: str) -> bool:
        """Make a dead row deliverable again with its counter reset; False if it cannot be."""
        ...

    async def mark_relayed(self, delivery_ids: Sequence[str], now: datetime) -> None:
        """Record that these ids were added to the stream."""
        ...

    async def list_relayable(
        self,
        now: datetime,
        *,
        mode: RelayMode,
        resweep_before: datetime | None = None,
        host: str | None = None,
        limit: int = 200,
    ) -> list[str]:
        """Ids of pending rows to add to the stream (see `is_relayable` for the rules)."""
        ...

    async def purge_bodies(self, processed_before: datetime, dead_before: datetime) -> int:
        """Null the body of processed rows older than one bound and dead rows older than another."""
        ...

    async def delete_expired(self, before: datetime) -> int:
        """Delete processed or dead rows received before `before` (the dedupe window ended)."""
        ...


def is_relayable(
    row: DeliveryRecord,
    now: datetime,
    *,
    mode: RelayMode,
    resweep_before: datetime | None,
    host: str | None,
) -> bool:
    """The relay rules, shared by the in-memory store and mirrored in SQL by `PgDeliveryStore`.

    - `tick`: never relayed and not delayed, or a delayed retry that is now due and was not
      relayed since it was scheduled.
    - `resweep`: every pending row not delayed into the future, relayed before
      `resweep_before` or never (the re-relay sweep after stream loss).
    - `host`: every pending row of `host` not delayed into the future (the host just became
      ready; its rows were skipped by the worker).
    """
    if row.status != PENDING:
        return False
    due = row.next_attempt_at is None or row.next_attempt_at <= now
    if mode == "host":
        return row.host == host and due
    if mode == "resweep":
        stale = row.last_relayed_at is None or (
            resweep_before is not None and row.last_relayed_at < resweep_before
        )
        return due and stale
    if row.next_attempt_at is None:
        return row.last_relayed_at is None
    return row.next_attempt_at <= now and (
        row.last_relayed_at is None or row.last_relayed_at < row.next_attempt_at
    )


@dataclass
class InMemoryDeliveryStore:
    """Process-local store for `MODE=mock` and unit tests."""

    rows: dict[str, DeliveryRecord] = field(default_factory=dict)

    async def insert(self, delivery: NewDelivery) -> bool:
        """Store a delivery unless its id exists."""
        if delivery.delivery_id in self.rows:
            return False
        self.rows[delivery.delivery_id] = DeliveryRecord(
            delivery_id=delivery.delivery_id,
            host=delivery.host,
            event=delivery.event,
            headers=dict(delivery.headers),
            body=delivery.body,
            received_at=delivery.received_at,
        )
        return True

    async def get(self, delivery_id: str) -> DeliveryRecord | None:
        """The row for `delivery_id`."""
        return self.rows.get(delivery_id)

    async def existing_ids(self, delivery_ids: Sequence[str]) -> set[str]:
        """Stored ids among `delivery_ids`."""
        return {i for i in delivery_ids if i in self.rows}

    def _set(self, delivery_id: str, **changes: object) -> None:
        """Replace fields of a row."""
        self.rows[delivery_id] = replace(self.rows[delivery_id], **changes)  # type: ignore[arg-type]

    async def mark_processed(self, delivery_id: str, now: datetime) -> None:
        """Mark processed."""
        if self.rows[delivery_id].status == PENDING:
            self._set(delivery_id, status=PROCESSED, processed_at=now, next_attempt_at=None)

    async def record_failure(
        self, delivery_id: str, error: str, now: datetime, next_attempt_at: datetime
    ) -> int:
        """Count a failure."""
        row = self.rows[delivery_id]
        self._set(
            delivery_id,
            attempts=row.attempts + 1,
            last_error=error,
            next_attempt_at=next_attempt_at,
        )
        return row.attempts + 1

    async def defer(self, delivery_id: str, until: datetime) -> None:
        """Delay without counting."""
        self._set(delivery_id, next_attempt_at=until)

    async def mark_dead(self, delivery_id: str, error: str, now: datetime) -> None:
        """Move to dead."""
        self._set(delivery_id, status=DEAD, dead_at=now, last_error=error, next_attempt_at=None)

    async def requeue(self, delivery_id: str) -> bool:
        """Requeue a dead row whose body is still stored."""
        row = self.rows.get(delivery_id)
        if row is None or row.status != DEAD or row.body is None:
            return False
        self._set(
            delivery_id,
            status=PENDING,
            attempts=0,
            dead_at=None,
            last_error=None,
            next_attempt_at=None,
            last_relayed_at=None,
        )
        return True

    async def mark_relayed(self, delivery_ids: Sequence[str], now: datetime) -> None:
        """Record relays."""
        for delivery_id in delivery_ids:
            if delivery_id in self.rows:
                self._set(delivery_id, last_relayed_at=now)

    async def list_relayable(
        self,
        now: datetime,
        *,
        mode: RelayMode,
        resweep_before: datetime | None = None,
        host: str | None = None,
        limit: int = 200,
    ) -> list[str]:
        """Relayable ids in arrival order."""
        ordered = sorted(self.rows.values(), key=lambda r: (r.received_at, r.delivery_id))
        found = [
            r.delivery_id
            for r in ordered
            if is_relayable(r, now, mode=mode, resweep_before=resweep_before, host=host)
        ]
        return found[:limit]

    async def purge_bodies(self, processed_before: datetime, dead_before: datetime) -> int:
        """Null expired bodies."""
        purged = 0
        for row in list(self.rows.values()):
            if row.body is None:
                continue
            if (row.status == PROCESSED and (row.processed_at or row.received_at) < processed_before) or (
                row.status == DEAD and (row.dead_at or row.received_at) < dead_before
            ):
                self._set(row.delivery_id, body=None)
                purged += 1
        return purged

    async def delete_expired(self, before: datetime) -> int:
        """Delete old finished rows whose body is already purged."""
        old = [
            r.delivery_id
            for r in self.rows.values()
            if r.status != PENDING and r.received_at < before and r.body is None
        ]
        for delivery_id in old:
            del self.rows[delivery_id]
        return len(old)


def _record(row: WebhookDelivery) -> DeliveryRecord:
    """Map an ORM row to the immutable record."""
    return DeliveryRecord(
        delivery_id=row.delivery_id,
        host=row.host,
        event=row.event,
        headers=dict(row.headers),
        body=row.body,
        received_at=row.received_at,
        status=row.status,
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at,
        last_relayed_at=row.last_relayed_at,
        processed_at=row.processed_at,
        dead_at=row.dead_at,
        last_error=row.last_error,
    )


class PgDeliveryStore:
    """Postgres-backed outbox; each method is one committed transaction."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Bind the session factory (the app's, so pooling and TLS settings apply)."""
        self._sf = session_factory

    async def insert(self, delivery: NewDelivery) -> bool:
        """`INSERT .. ON CONFLICT DO NOTHING`; True when a row was created."""
        stmt = (
            pg_insert(WebhookDelivery)
            .values(
                delivery_id=delivery.delivery_id,
                host=delivery.host,
                event=delivery.event,
                headers=delivery.headers,
                body=delivery.body,
                received_at=delivery.received_at,
            )
            .on_conflict_do_nothing(index_elements=["delivery_id"])
            .returning(WebhookDelivery.delivery_id)
        )
        async with self._sf() as session, session.begin():
            return (await session.execute(stmt)).first() is not None

    async def get(self, delivery_id: str) -> DeliveryRecord | None:
        """Load a row."""
        async with self._sf() as session:
            row = await session.get(WebhookDelivery, delivery_id)
            return None if row is None else _record(row)

    async def existing_ids(self, delivery_ids: Sequence[str]) -> set[str]:
        """Stored ids among `delivery_ids`."""
        if not delivery_ids:
            return set()
        stmt = select(WebhookDelivery.delivery_id).where(
            WebhookDelivery.delivery_id.in_(list(delivery_ids))
        )
        async with self._sf() as session:
            return set((await session.scalars(stmt)).all())

    async def _exec(self, stmt: object) -> int:
        """Run a write statement in its own transaction; returns the row count."""
        async with self._sf() as session, session.begin():
            result = await session.execute(stmt)  # type: ignore[call-overload]
            return int(result.rowcount)

    async def mark_processed(self, delivery_id: str, now: datetime) -> None:
        """Mark processed."""
        await self._exec(
            update(WebhookDelivery)
            .where(WebhookDelivery.delivery_id == delivery_id, WebhookDelivery.status == PENDING)
            .values(status=PROCESSED, processed_at=now, next_attempt_at=None)
        )

    async def record_failure(
        self, delivery_id: str, error: str, now: datetime, next_attempt_at: datetime
    ) -> int:
        """Count a failure; returns the new counter."""
        stmt = (
            update(WebhookDelivery)
            .where(WebhookDelivery.delivery_id == delivery_id, WebhookDelivery.status == PENDING)
            .values(
                attempts=WebhookDelivery.attempts + 1,
                last_error=error,
                next_attempt_at=next_attempt_at,
            )
            .returning(WebhookDelivery.attempts)
        )
        async with self._sf() as session, session.begin():
            value = (await session.execute(stmt)).scalar_one_or_none()
        return int(value or 0)

    async def defer(self, delivery_id: str, until: datetime) -> None:
        """Delay without counting."""
        await self._exec(
            update(WebhookDelivery)
            .where(WebhookDelivery.delivery_id == delivery_id, WebhookDelivery.status == PENDING)
            .values(next_attempt_at=until)
        )

    async def mark_dead(self, delivery_id: str, error: str, now: datetime) -> None:
        """Move a pending row to dead."""
        await self._exec(
            update(WebhookDelivery)
            .where(WebhookDelivery.delivery_id == delivery_id, WebhookDelivery.status == PENDING)
            .values(status=DEAD, dead_at=now, last_error=error, next_attempt_at=None)
        )

    async def requeue(self, delivery_id: str) -> bool:
        """Requeue a dead row whose body still exists, resetting its counter."""
        count = await self._exec(
            update(WebhookDelivery)
            .where(
                WebhookDelivery.delivery_id == delivery_id,
                WebhookDelivery.status == DEAD,
                WebhookDelivery.body.is_not(None),
            )
            .values(
                status=PENDING,
                attempts=0,
                dead_at=None,
                last_error=None,
                next_attempt_at=None,
                last_relayed_at=None,
            )
        )
        return count == 1

    async def mark_relayed(self, delivery_ids: Sequence[str], now: datetime) -> None:
        """Record relays."""
        if delivery_ids:
            await self._exec(
                update(WebhookDelivery)
                .where(WebhookDelivery.delivery_id.in_(list(delivery_ids)))
                .values(last_relayed_at=now)
            )

    async def list_relayable(
        self,
        now: datetime,
        *,
        mode: RelayMode,
        resweep_before: datetime | None = None,
        host: str | None = None,
        limit: int = 200,
    ) -> list[str]:
        """The SQL form of `is_relayable`."""
        t = WebhookDelivery
        due = or_(t.next_attempt_at.is_(None), t.next_attempt_at <= now)
        if mode == "host":
            cond = and_(t.host == host, due)
        elif mode == "resweep":
            stale = t.last_relayed_at.is_(None)
            if resweep_before is not None:
                stale = or_(stale, t.last_relayed_at < resweep_before)
            cond = and_(due, stale)
        else:
            cond = or_(
                and_(t.next_attempt_at.is_(None), t.last_relayed_at.is_(None)),
                and_(
                    t.next_attempt_at <= now,
                    or_(t.last_relayed_at.is_(None), t.last_relayed_at < t.next_attempt_at),
                ),
            )
        stmt = (
            select(t.delivery_id)
            .where(t.status == PENDING, cond)
            .order_by(t.received_at, t.delivery_id)
            .limit(limit)
        )
        async with self._sf() as session:
            return list((await session.scalars(stmt)).all())

    async def purge_bodies(self, processed_before: datetime, dead_before: datetime) -> int:
        """Null expired bodies; the rows stay to deduplicate a late redelivery."""
        t = WebhookDelivery
        return await self._exec(
            update(t)
            .where(
                t.body.is_not(None),
                or_(
                    and_(t.status == PROCESSED, t.processed_at < processed_before),
                    and_(t.status == DEAD, t.dead_at < dead_before),
                ),
            )
            .values(body=None)
        )

    async def delete_expired(self, before: datetime) -> int:
        """Delete finished rows older than the dedupe window."""
        t = WebhookDelivery
        return await self._exec(
            delete(t).where(t.status != PENDING, t.received_at < before, t.body.is_(None))
        )


def backoff_seconds(attempts: int, base: float, cap: float) -> float:
    """Exponential retry backoff: `base * 2**(attempts - 1)`, capped at `cap` (attempts >= 1)."""
    exponent = max(attempts - 1, 0)
    return float(min(cap, base * (2**exponent)))


def retry_at(now: datetime, attempts: int, base: float, cap: float) -> datetime:
    """When a delivery that has failed `attempts` times is retried."""
    return now + timedelta(seconds=backoff_seconds(attempts, base, cap))
