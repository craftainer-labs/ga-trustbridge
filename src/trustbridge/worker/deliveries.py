"""The delivery outbox: records, the `DeliveryStore` Protocol, Postgres and in-memory stores.

`webhook_deliveries` is the source of truth (ADR 0028 "Request path and processing"). The
Postgres store commits each write in its own transaction before returning, so ingress can
answer 2xx only after the row is durable. The in-memory store backs `MODE=mock` and unit
tests; the Postgres store is covered by the integration tests.
"""

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Literal, NamedTuple, Protocol

from sqlalchemy import ColumnElement, Update, and_, delete, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.worker.models import WebhookDelivery

_log = logging.getLogger(__name__)

RelayMode = Literal["tick", "resweep", "host"]
PENDING = "pending"
PROCESSED = "processed"
DEAD = "dead"
DUPLICATE_BODY = "duplicate body of delivery"  # `last_error` of a terminal replay-marker row


class DeliveryKey(NamedTuple):
    """The identity of a delivery: GitHub's delivery id is unique per host, not globally."""

    host: str
    delivery_id: str


@dataclass(frozen=True, slots=True)
class NewDelivery:
    """What ingress stores: id, host, event, allow-listed headers and the body."""

    delivery_id: str
    host: str
    event: str
    headers: dict[str, str]
    body: bytes
    received_at: datetime

    @property
    def key(self) -> DeliveryKey:
        """The `(host, delivery_id)` identity."""
        return DeliveryKey(self.host, self.delivery_id)

    @property
    def body_sha256(self) -> str:
        """Hex SHA-256 of the raw signed body: the replay-dedupe identity (per host)."""
        return hashlib.sha256(self.body).hexdigest()


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
    body_sha256: str | None = None

    @property
    def key(self) -> DeliveryKey:
        """The `(host, delivery_id)` identity."""
        return DeliveryKey(self.host, self.delivery_id)


class DeliveryStore(Protocol):
    """Persistence of the outbox; every method commits before it returns."""

    async def insert(self, delivery: NewDelivery) -> bool:
        """Store a delivery; False if its `(host, id)` or body hash already exists.

        `X-GitHub-Delivery` is not covered by the HMAC signature, so the same signed body
        replayed under a fresh delivery id is also a duplicate (per host, for the row's life).
        A replayed body still gets a row for its own id, already `processed`, with no body and
        `body_sha256` NULL (NULLs never conflict) and `last_error` naming the first delivery,
        so the reconciler sees the GUID as received and never asks for it again.
        """
        ...

    async def get(self, key: DeliveryKey) -> DeliveryRecord | None:
        """The row for `key`, or None."""
        ...

    async def existing_keys(self, keys: Sequence[DeliveryKey]) -> set[DeliveryKey]:
        """The subset of `keys` that has a stored row."""
        ...

    async def mark_processed(self, key: DeliveryKey, now: datetime, *, attempts: int) -> bool:
        """Mark a row processed only if it is still `pending` with this `attempts` (its claim).

        `attempts` is the row's generation: it changes with every counted failure and a requeue
        resets it, so a claim whose row moved on affects nothing. Returns whether it updated.
        """
        ...

    async def record_failure(
        self,
        key: DeliveryKey,
        error: str,
        now: datetime,
        next_attempt_at: datetime,
        *,
        attempts: int,
    ) -> int:
        """Count one failure of the claim `attempts` and schedule the retry.

        Returns the new counter, or 0 when the claim was lost (the row is no longer `pending`
        with this `attempts`), in which case nothing changes.
        """
        ...

    async def defer(self, key: DeliveryKey, until: datetime) -> None:
        """Delay a row without counting a failure (busy lease)."""
        ...

    async def mark_dead(
        self, key: DeliveryKey, error: str, now: datetime, *, attempts: int
    ) -> bool:
        """Move a row to `dead` only if it is still `pending` with this `attempts` (its claim)."""
        ...

    async def requeue(self, key: DeliveryKey) -> bool:
        """Make a dead row deliverable again with its counter reset; False if it cannot be."""
        ...

    async def mark_relayed(self, keys: Sequence[DeliveryKey], now: datetime) -> None:
        """Record that these deliveries were added to the stream."""
        ...

    async def list_relayable(
        self,
        now: datetime,
        *,
        mode: RelayMode,
        resweep_before: datetime | None = None,
        host: str | None = None,
        limit: int = 200,
    ) -> list[DeliveryKey]:
        """Keys of pending rows to add to the stream (see `is_relayable` for the rules)."""
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

    rows: dict[DeliveryKey, DeliveryRecord] = field(default_factory=dict)

    async def insert(self, delivery: NewDelivery) -> bool:
        """Store a delivery unless its `(host, id)` or its body hash exists on that host."""
        digest = delivery.body_sha256
        if delivery.key in self.rows:
            return False
        first = next(
            (r for r in self.rows.values() if r.host == delivery.host and r.body_sha256 == digest),
            None,
        )
        if first is not None:
            _log.warning(
                "replayed webhook body on %s: delivery %s repeats delivery %s",
                delivery.host,
                delivery.delivery_id,
                first.delivery_id,
            )
            self.rows[delivery.key] = DeliveryRecord(
                delivery_id=delivery.delivery_id,
                host=delivery.host,
                event=delivery.event,
                headers=dict(delivery.headers),
                body=None,
                received_at=delivery.received_at,
                status=PROCESSED,
                processed_at=delivery.received_at,
                last_error=f"{DUPLICATE_BODY} {first.delivery_id}",
                body_sha256=None,
            )
            return False
        self.rows[delivery.key] = DeliveryRecord(
            delivery_id=delivery.delivery_id,
            host=delivery.host,
            event=delivery.event,
            headers=dict(delivery.headers),
            body=delivery.body,
            received_at=delivery.received_at,
            body_sha256=digest,
        )
        return True

    async def get(self, key: DeliveryKey) -> DeliveryRecord | None:
        """The row for `key`."""
        return self.rows.get(key)

    async def existing_keys(self, keys: Sequence[DeliveryKey]) -> set[DeliveryKey]:
        """Stored keys among `keys`."""
        return {k for k in keys if k in self.rows}

    def _set(self, key: DeliveryKey, **changes: object) -> None:
        """Replace fields of a row."""
        self.rows[key] = replace(self.rows[key], **changes)  # type: ignore[arg-type]

    async def mark_processed(self, key: DeliveryKey, now: datetime, *, attempts: int) -> bool:
        """Mark processed if the claim still holds."""
        row = self.rows[key]
        if row.status != PENDING or row.attempts != attempts:
            return False
        self._set(key, status=PROCESSED, processed_at=now, next_attempt_at=None)
        return True

    async def record_failure(
        self,
        key: DeliveryKey,
        error: str,
        now: datetime,
        next_attempt_at: datetime,
        *,
        attempts: int,
    ) -> int:
        """Count a failure if the claim still holds."""
        row = self.rows[key]
        if row.status != PENDING or row.attempts != attempts:
            return 0
        self._set(
            key,
            attempts=row.attempts + 1,
            last_error=error,
            next_attempt_at=next_attempt_at,
        )
        return row.attempts + 1

    async def defer(self, key: DeliveryKey, until: datetime) -> None:
        """Delay without counting."""
        self._set(key, next_attempt_at=until)

    async def mark_dead(
        self, key: DeliveryKey, error: str, now: datetime, *, attempts: int
    ) -> bool:
        """Move to dead if the claim still holds."""
        row = self.rows[key]
        if row.status != PENDING or row.attempts != attempts:
            return False
        self._set(key, status=DEAD, dead_at=now, last_error=error, next_attempt_at=None)
        return True

    async def requeue(self, key: DeliveryKey) -> bool:
        """Requeue a dead row whose body is still stored."""
        row = self.rows.get(key)
        if row is None or row.status != DEAD or row.body is None:
            return False
        self._set(
            key,
            status=PENDING,
            attempts=0,
            dead_at=None,
            last_error=None,
            next_attempt_at=None,
            last_relayed_at=None,
        )
        return True

    async def mark_relayed(self, keys: Sequence[DeliveryKey], now: datetime) -> None:
        """Record relays."""
        for key in keys:
            if key in self.rows:
                self._set(key, last_relayed_at=now)

    async def list_relayable(
        self,
        now: datetime,
        *,
        mode: RelayMode,
        resweep_before: datetime | None = None,
        host: str | None = None,
        limit: int = 200,
    ) -> list[DeliveryKey]:
        """Relayable keys in arrival order."""
        ordered = sorted(self.rows.values(), key=lambda r: (r.received_at, r.host, r.delivery_id))
        found = [
            r.key
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
            if (
                row.status == PROCESSED and (row.processed_at or row.received_at) < processed_before
            ) or (row.status == DEAD and (row.dead_at or row.received_at) < dead_before):
                self._set(row.key, body=None)
                purged += 1
        return purged

    async def delete_expired(self, before: datetime) -> int:
        """Delete old finished rows whose body is already purged."""
        old = [
            r.key
            for r in self.rows.values()
            if r.status != PENDING and r.received_at < before and r.body is None
        ]
        for key in old:
            del self.rows[key]
        return len(old)


def _is(key: DeliveryKey) -> ColumnElement[bool]:
    """The predicate selecting the row of `key`."""
    return and_(WebhookDelivery.host == key.host, WebhookDelivery.delivery_id == key.delivery_id)


def _in_keys(keys: Sequence[DeliveryKey]) -> ColumnElement[bool]:
    """The predicate selecting the rows of any of `keys`."""
    return tuple_(WebhookDelivery.host, WebhookDelivery.delivery_id).in_(
        [(k.host, k.delivery_id) for k in keys]
    )


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
        body_sha256=row.body_sha256,
    )


def requeue_statement(key: DeliveryKey) -> Update:
    """The one UPDATE that makes a dead row with a body deliverable again (counter reset).

    `PgDeliveryStore.requeue` runs it in its own transaction; the admin API runs the same
    statement in the request's transaction, so the audit event commits with the change.
    """
    return (
        update(WebhookDelivery)
        .where(
            _is(key),
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


class PgDeliveryStore:
    """Postgres-backed outbox; each method is one committed transaction."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Bind the session factory (the app's, so pooling and TLS settings apply)."""
        self._sf = session_factory

    async def insert(self, delivery: NewDelivery) -> bool:
        """Insert `(host, id)`; True when a new row is created, False for a redelivery or replay.

        The same id is a redelivery (`ON CONFLICT (host, delivery_id) DO NOTHING`). A body
        already stored under another id is a replay: it is logged with the first id and gets a
        terminal marker row instead. Two concurrent first inserts of one body race on the
        `(host, body_sha256)` index; the loser retries once and takes the replay path.
        """
        try:
            async with self._sf() as session, session.begin():
                return await self._insert_once(session, delivery)
        except IntegrityError:
            _log.info("lost a body-hash race for delivery %s; retrying", delivery.delivery_id)
        async with self._sf() as session, session.begin():
            return await self._insert_once(session, delivery)

    @staticmethod
    async def _insert_once(session: AsyncSession, delivery: NewDelivery) -> bool:
        digest = delivery.body_sha256
        first = (
            await session.scalars(
                select(WebhookDelivery.delivery_id).where(
                    WebhookDelivery.host == delivery.host, WebhookDelivery.body_sha256 == digest
                )
            )
        ).first()
        base = {
            "delivery_id": delivery.delivery_id,
            "host": delivery.host,
            "event": delivery.event,
            "headers": delivery.headers,
            "received_at": delivery.received_at,
        }
        if first is not None and first != delivery.delivery_id:
            _log.warning(
                "replayed webhook body on %s: delivery %s repeats delivery %s",
                delivery.host,
                delivery.delivery_id,
                first,
            )
            marker = (
                pg_insert(WebhookDelivery)
                .values(
                    **base,
                    body=None,
                    status=PROCESSED,
                    processed_at=delivery.received_at,
                    last_error=f"{DUPLICATE_BODY} {first}",
                    body_sha256=None,
                )
                .on_conflict_do_nothing(index_elements=["host", "delivery_id"])
            )
            await session.execute(marker)
            return False
        stmt = (
            pg_insert(WebhookDelivery)
            .values(**base, body=delivery.body, body_sha256=digest)
            .on_conflict_do_nothing(index_elements=["host", "delivery_id"])
            .returning(WebhookDelivery.delivery_id)
        )
        return (await session.execute(stmt)).first() is not None

    async def get(self, key: DeliveryKey) -> DeliveryRecord | None:
        """Load a row."""
        async with self._sf() as session:
            row = (await session.scalars(select(WebhookDelivery).where(_is(key)))).one_or_none()
            return None if row is None else _record(row)

    async def existing_keys(self, keys: Sequence[DeliveryKey]) -> set[DeliveryKey]:
        """Stored keys among `keys`."""
        if not keys:
            return set()
        stmt = select(WebhookDelivery.host, WebhookDelivery.delivery_id).where(_in_keys(keys))
        async with self._sf() as session:
            return {DeliveryKey(h, i) for h, i in (await session.execute(stmt)).all()}

    async def _exec(self, stmt: object) -> int:
        """Run a write statement in its own transaction; returns the row count."""
        async with self._sf() as session, session.begin():
            result = await session.execute(stmt)  # type: ignore[call-overload]
            return int(result.rowcount)

    async def mark_processed(self, key: DeliveryKey, now: datetime, *, attempts: int) -> bool:
        """Mark processed if the row is still pending with the claimed `attempts`."""
        count = await self._exec(
            update(WebhookDelivery)
            .where(
                _is(key), WebhookDelivery.status == PENDING, WebhookDelivery.attempts == attempts
            )
            .values(status=PROCESSED, processed_at=now, next_attempt_at=None)
        )
        return count == 1

    async def record_failure(
        self,
        key: DeliveryKey,
        error: str,
        now: datetime,
        next_attempt_at: datetime,
        *,
        attempts: int,
    ) -> int:
        """Count a failure of the claimed `attempts`; returns the new counter, 0 if lost."""
        stmt = (
            update(WebhookDelivery)
            .where(
                _is(key), WebhookDelivery.status == PENDING, WebhookDelivery.attempts == attempts
            )
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

    async def defer(self, key: DeliveryKey, until: datetime) -> None:
        """Delay without counting."""
        await self._exec(
            update(WebhookDelivery)
            .where(_is(key), WebhookDelivery.status == PENDING)
            .values(next_attempt_at=until)
        )

    async def mark_dead(
        self, key: DeliveryKey, error: str, now: datetime, *, attempts: int
    ) -> bool:
        """Move a pending row to dead if it still has the claimed `attempts`."""
        count = await self._exec(
            update(WebhookDelivery)
            .where(
                _is(key), WebhookDelivery.status == PENDING, WebhookDelivery.attempts == attempts
            )
            .values(status=DEAD, dead_at=now, last_error=error, next_attempt_at=None)
        )
        return count == 1

    async def requeue(self, key: DeliveryKey) -> bool:
        """Requeue a dead row whose body still exists, resetting its counter."""
        return await self._exec(requeue_statement(key)) == 1

    async def mark_relayed(self, keys: Sequence[DeliveryKey], now: datetime) -> None:
        """Record relays."""
        if keys:
            await self._exec(
                update(WebhookDelivery).where(_in_keys(keys)).values(last_relayed_at=now)
            )

    async def list_relayable(
        self,
        now: datetime,
        *,
        mode: RelayMode,
        resweep_before: datetime | None = None,
        host: str | None = None,
        limit: int = 200,
    ) -> list[DeliveryKey]:
        """The SQL form of `is_relayable`."""
        t = WebhookDelivery
        due = or_(t.next_attempt_at.is_(None), t.next_attempt_at <= now)
        if mode == "host":
            cond = and_(t.host == host, due)
        elif mode == "resweep":
            stale: ColumnElement[bool] = t.last_relayed_at.is_(None)
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
            select(t.host, t.delivery_id)
            .where(t.status == PENDING, cond)
            .order_by(t.received_at, t.host, t.delivery_id)
            .limit(limit)
        )
        async with self._sf() as session:
            return [DeliveryKey(h, i) for h, i in (await session.execute(stmt)).all()]

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


class TenantGate(Protocol):
    """Whether a tenant is admin-disabled (ADR 0033 "Tenant disable"); read by the worker."""

    async def disabled(self, host: str, installation_id: int) -> bool:
        """True when the tenant of `(host, installation_id)` is admin-disabled."""
        ...
