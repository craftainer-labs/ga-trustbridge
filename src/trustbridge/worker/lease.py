"""Scope leases with fencing (ADR 0028 "Persistence and serialisation"; invariants 9-11).

Interface for NG4 and the adapter core:

- `LeaseManager(store, ttl_seconds=...)`; `async with manager.hold(tenant_id, adapter, key) as
  lease:` takes the lease for `(tenant, adapter, lease key)` or raises `LeaseBusy` (not a
  delivery failure: the worker defers the delivery without counting it). The holder identity
  is minted per call (`new_holder_id()`), never per process or consumer, so two tasks in one
  process, or a redelivery to the same consumer, cannot both hold it. A background task renews
  the lease; if it is lost, the holding task is cancelled and `LeaseLost` is raised on exit.
- Guarded writes: `lease.valid_clause()` is an `EXISTS` subquery on the lease row that checks
  holder, fence **and** `expires_at > clock_timestamp()`. `guarded_execute(session, lease,
  stmt)` adds it to an UPDATE or DELETE, and `guarded_insert(session, lease, table, values)`
  inserts through `INSERT .. SELECT .. WHERE EXISTS(..)`, so the fence is checked in the same
  statement as the write; zero rows affected raises `LeaseLost`. Renewing and releasing never
  change the fence the holder keeps; a takeover after expiry increments it.
- `bounded_call(awaitable, settings.adapter_call_timeout_seconds)` bounds a platform call;
  the settings validator guarantees that timeout is shorter than `lease_ttl_seconds`.
- `Lease.guard()` is `valid_clause()` under the adapter core's `Lease`/`LeaseProvider`
  Protocols (`LeaseManager.hold` is called with keyword arguments there).
- `NIL_TENANT` (from `trustbridge.worker.models`) is the tenant id for core-wide keys.

Time is the database clock (`clock_timestamp()`), so process clock skew cannot extend a lease.
"""

import asyncio
import contextlib
import logging
import os
import socket
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from sqlalchemy import ColumnElement, Delete, Table, Update, exists, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.worker.models import ScopeLease

logger = logging.getLogger(__name__)


class LeaseBusy(Exception):  # noqa: N818 -- a control-flow signal, not a failure
    """The lease is held by another unit of work."""


class LeaseLost(Exception):  # noqa: N818 -- a control-flow signal, not a failure
    """The lease expired or was taken over; a guarded write must not commit."""


def new_holder_id() -> str:
    """A holder identity unique to one unit of work."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"


@dataclass(frozen=True, slots=True)
class Lease:
    """A held lease; `fence` is fixed for the holder's lifetime."""

    tenant_id: uuid.UUID
    adapter: str
    key: str
    holder: str
    fence: int

    def valid_clause(self) -> ColumnElement[bool]:
        """`EXISTS` of this exact (holder, fence) row that has not expired, at statement time."""
        t = ScopeLease
        return (
            exists()
            .where(
                t.tenant_id == self.tenant_id,
                t.adapter == self.adapter,
                t.lease_key == self.key,
                t.holder == self.holder,
                t.fence == self.fence,
                t.expires_at > func.clock_timestamp(),
            )
            .correlate(None)
        )

    def guard(self) -> ColumnElement[bool]:
        """The adapter core's name for `valid_clause` (its `Lease` Protocol)."""
        return self.valid_clause()


class LeaseStore(Protocol):
    """Persistence of lease rows."""

    async def acquire(
        self, tenant_id: uuid.UUID, adapter: str, key: str, holder: str, ttl: float
    ) -> int | None:
        """Take the lease if free or expired, returning the new fence; None if held."""
        ...

    async def renew(self, lease: Lease, ttl: float) -> bool:
        """Extend a still-valid lease of this holder and fence; False if it is gone."""
        ...

    async def release(self, lease: Lease) -> None:
        """Expire the lease now if still held with this fence (the fence is unchanged)."""
        ...


class PgLeaseStore:
    """`scope_leases` in Postgres; every method is one statement in its own transaction."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Bind the session factory."""
        self._sf = session_factory

    async def acquire(
        self, tenant_id: uuid.UUID, adapter: str, key: str, holder: str, ttl: float
    ) -> int | None:
        """Upsert that only takes over an expired row, bumping its fence."""
        expires = func.clock_timestamp() + timedelta(seconds=ttl)
        ins = pg_insert(ScopeLease).values(
            tenant_id=tenant_id,
            adapter=adapter,
            lease_key=key,
            holder=holder,
            fence=1,
            expires_at=expires,
        )
        stmt = ins.on_conflict_do_update(
            index_elements=[ScopeLease.tenant_id, ScopeLease.adapter, ScopeLease.lease_key],
            set_={
                "holder": ins.excluded.holder,
                "fence": ScopeLease.fence + 1,
                "expires_at": ins.excluded.expires_at,
            },
            where=ScopeLease.expires_at <= func.clock_timestamp(),
        ).returning(ScopeLease.fence)
        async with self._sf() as session, session.begin():
            row = (await session.execute(stmt)).first()
        return None if row is None else int(row[0])

    def _same(self, lease: Lease) -> tuple[ColumnElement[bool], ...]:
        """Predicates selecting exactly this holder's row at this fence."""
        t = ScopeLease
        return (
            t.tenant_id == lease.tenant_id,
            t.adapter == lease.adapter,
            t.lease_key == lease.key,
            t.holder == lease.holder,
            t.fence == lease.fence,
        )

    async def renew(self, lease: Lease, ttl: float) -> bool:
        """Extend only an unexpired lease; the fence column is not touched."""
        stmt = (
            update(ScopeLease)
            .where(*self._same(lease), ScopeLease.expires_at > func.clock_timestamp())
            .values(expires_at=func.clock_timestamp() + timedelta(seconds=ttl))
            .returning(ScopeLease.fence)
        )
        async with self._sf() as session, session.begin():
            return (await session.execute(stmt)).first() is not None

    async def release(self, lease: Lease) -> None:
        """Expire the row now (it stays, so the next takeover increments the fence)."""
        stmt = (
            update(ScopeLease).where(*self._same(lease)).values(expires_at=func.clock_timestamp())
        )
        async with self._sf() as session, session.begin():
            await session.execute(stmt)


class InMemoryLeaseStore:
    """Process-local lease store with an injectable clock, for unit tests and `MODE=mock`."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """Use `clock` (seconds) as the time source."""
        self._clock = clock
        self._rows: dict[tuple[uuid.UUID, str, str], tuple[str, int, float]] = {}

    async def acquire(
        self, tenant_id: uuid.UUID, adapter: str, key: str, holder: str, ttl: float
    ) -> int | None:
        """Take a free or expired lease."""
        row_key = (tenant_id, adapter, key)
        current = self._rows.get(row_key)
        if current is not None and current[2] > self._clock():
            return None
        fence = 1 if current is None else current[1] + 1
        self._rows[row_key] = (holder, fence, self._clock() + ttl)
        return fence

    async def renew(self, lease: Lease, ttl: float) -> bool:
        """Extend an unexpired lease of this holder and fence."""
        if not self.is_valid(lease):
            return False
        self._rows[(lease.tenant_id, lease.adapter, lease.key)] = (
            lease.holder,
            lease.fence,
            self._clock() + ttl,
        )
        return True

    async def release(self, lease: Lease) -> None:
        """Expire the lease now if this holder and fence still own it."""
        row_key = (lease.tenant_id, lease.adapter, lease.key)
        current = self._rows.get(row_key)
        if current is not None and current[:2] == (lease.holder, lease.fence):
            self._rows[row_key] = (current[0], current[1], self._clock())

    def is_valid(self, lease: Lease) -> bool:
        """Whether `lease` is the current, unexpired holder (what `valid_clause` does in SQL)."""
        current = self._rows.get((lease.tenant_id, lease.adapter, lease.key))
        return (
            current is not None
            and current[:2] == (lease.holder, lease.fence)
            and current[2] > self._clock()
        )


class LeaseManager:
    """Acquire, keep alive and release leases for units of work."""

    def __init__(self, store: LeaseStore, *, ttl_seconds: float) -> None:
        """Bind the store and the lease TTL."""
        self._store = store
        self.ttl = ttl_seconds

    @asynccontextmanager
    async def hold(self, tenant_id: uuid.UUID, adapter: str, key: str) -> AsyncIterator[Lease]:
        """Hold the lease for the body; raise `LeaseBusy` if taken, `LeaseLost` if it is lost."""
        holder = new_holder_id()
        fence = await self._store.acquire(tenant_id, adapter, key, holder, self.ttl)
        if fence is None:
            raise LeaseBusy(f"{adapter}/{key}")
        lease = Lease(tenant_id, adapter, key, holder, fence)
        owner = asyncio.current_task()
        if owner is None:  # pragma: no cover -- a coroutine always runs inside a task
            raise RuntimeError("LeaseManager.hold must run inside a task")
        lost = asyncio.Event()

        async def _keepalive() -> None:
            while True:
                await asyncio.sleep(self.ttl / 3)
                try:
                    alive = await self._store.renew(lease, self.ttl)
                except Exception:
                    logger.exception("lease renew failed", extra={"adapter": adapter})
                    continue
                if not alive:
                    lost.set()
                    owner.cancel()
                    return

        keepalive = asyncio.create_task(_keepalive())
        try:
            yield lease
        except asyncio.CancelledError:
            if lost.is_set():
                owner.uncancel()  # the cancellation was ours; it becomes LeaseLost
                raise LeaseLost(f"{adapter}/{key}") from None
            raise
        finally:
            keepalive.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keepalive
            # Releasing is a no-op for a lost lease (the holder and fence no longer match).
            with contextlib.suppress(Exception):
                await asyncio.shield(self._store.release(lease))


async def bounded_call[T](awaitable: Awaitable[T], timeout: float) -> T:
    """Await `awaitable` for at most `timeout` seconds (shorter than the lease TTL)."""
    async with asyncio.timeout(timeout):
        return await awaitable


async def guarded_execute(
    session: AsyncSession, lease: Lease, stmt: Update | Delete, *, require_row: bool = True
) -> int:
    """Run an UPDATE or DELETE with the lease check in the same statement.

    Returns the affected row count; raises `LeaseLost` when no row was affected and
    `require_row` is true (the default: a guarded write that matches nothing means the fence
    failed or the target vanished, and the caller must not treat the work as done).
    """
    result = await session.execute(stmt.where(lease.valid_clause()))
    count = int(result.rowcount)  # type: ignore[attr-defined]
    if require_row and count == 0:
        raise LeaseLost(f"{lease.adapter}/{lease.key}")
    return count


async def guarded_insert(
    session: AsyncSession, lease: Lease, table: Table, values: dict[str, Any]
) -> None:
    """`INSERT .. SELECT .. WHERE EXISTS(lease)`: inserts one row only while the lease is valid."""
    columns = list(values)
    source = select(*(literal(values[c], table.c[c].type) for c in columns)).where(
        lease.valid_clause()
    )
    result = await session.execute(table.insert().from_select(columns, source))
    if int(result.rowcount) == 0:  # type: ignore[attr-defined]
        raise LeaseLost(f"{lease.adapter}/{lease.key}")
