"""Leases with fencing (0028 invariants 9, 10 and 11).

The SQL itself runs in the integration tests.
"""

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.dialects import postgresql

from trustbridge.worker.lease import (
    InMemoryLeaseStore,
    Lease,
    LeaseBusy,
    LeaseLost,
    LeaseManager,
    bounded_call,
    guarded_execute,
    guarded_insert,
    new_holder_id,
)
from trustbridge.worker.models import WebhookDelivery

TENANT = uuid.uuid4()


class Time:
    """A manually advanced clock for the in-memory store."""

    def __init__(self) -> None:
        """Initialise."""
        self.t = 100.0

    def __call__(self) -> float:
        """Implement __call__."""
        return self.t


async def test_at_most_one_unit_of_work_holds_a_lease() -> None:  # inv. 9
    """At most one unit of work holds a lease."""
    manager = LeaseManager(InMemoryLeaseStore(), ttl_seconds=120)
    entered, release = asyncio.Event(), asyncio.Event()

    async def first() -> None:
        async with manager.hold(TENANT, "jfrog", "scope-1"):
            entered.set()
            await release.wait()

    holder = asyncio.create_task(first())
    await entered.wait()
    with pytest.raises(LeaseBusy):  # a second task in the same process
        async with manager.hold(TENANT, "jfrog", "scope-1"):
            pytest.fail("two units of work held one lease")
    async with manager.hold(TENANT, "jfrog", "other-scope"):  # a different key is independent
        pass
    async with manager.hold(uuid.uuid4(), "jfrog", "scope-1"):  # and so is a different tenant
        pass
    release.set()
    await holder
    async with manager.hold(TENANT, "jfrog", "scope-1"):  # free again after release
        pass


def test_holder_identity_is_per_unit_of_work() -> None:  # inv. 9
    """Holder identity is per unit of work."""
    assert len({new_holder_id() for _ in range(200)}) == 200


async def test_a_redelivery_to_the_same_consumer_cannot_also_hold_the_lease() -> None:  # inv. 9
    """A redelivery to the same consumer cannot also hold the lease."""
    manager = LeaseManager(InMemoryLeaseStore(), ttl_seconds=120)
    async with manager.hold(TENANT, "a", "k"):
        with pytest.raises(LeaseBusy):
            async with manager.hold(TENANT, "a", "k"):  # same process, same "consumer"
                pass


async def test_takeover_after_expiry_bumps_the_fence_and_locks_out_the_old_holder() -> None:
    """Takeover after expiry bumps the fence and locks out the old holder."""
    clock = Time()  # inv. 10
    store = InMemoryLeaseStore(clock)
    fence_a = await store.acquire(TENANT, "a", "k", "holder-a", ttl=10)
    assert fence_a == 1
    assert await store.acquire(TENANT, "a", "k", "holder-b", ttl=10) is None  # still held
    clock.t += 11
    fence_b = await store.acquire(TENANT, "a", "k", "holder-b", ttl=10)
    assert fence_b == 2
    old = Lease(TENANT, "a", "k", "holder-a", 1)
    new = Lease(TENANT, "a", "k", "holder-b", 2)
    assert not store.is_valid(old)
    assert store.is_valid(new)
    assert not await store.renew(old, 10)
    await store.release(old)  # a stale release must not free the new holder's lease
    assert store.is_valid(new)


async def test_an_expired_lease_is_invalid_even_before_anyone_takes_it_over() -> None:  # inv. 10
    """An expired lease is invalid even before anyone takes it over."""
    clock = Time()
    store = InMemoryLeaseStore(clock)
    await store.acquire(TENANT, "a", "k", "h", ttl=10)
    lease = Lease(TENANT, "a", "k", "h", 1)
    assert store.is_valid(lease)
    clock.t += 10
    assert not store.is_valid(lease)
    assert not await store.renew(lease, 10)


async def test_renew_and_release_never_change_the_fence() -> None:  # inv. 10
    """Renew and release never change the fence."""
    clock = Time()
    store = InMemoryLeaseStore(clock)
    assert await store.acquire(TENANT, "a", "k", "h", ttl=10) == 1
    lease = Lease(TENANT, "a", "k", "h", 1)
    clock.t += 5
    assert await store.renew(lease, 10)
    assert store.is_valid(lease)
    await store.release(lease)
    assert not store.is_valid(lease)
    assert await store.acquire(TENANT, "a", "k", "h2", ttl=10) == 2  # only a takeover bumps it


async def test_a_holder_that_lost_its_lease_gets_lease_lost() -> None:  # inv. 10
    """A holder that lost its lease gets lease lost."""
    store = InMemoryLeaseStore()
    manager = LeaseManager(store, ttl_seconds=0.3)
    with pytest.raises(LeaseLost):
        async with manager.hold(TENANT, "a", "k"):
            store._rows[(TENANT, "a", "k")] = ("thief", 99, 10**9)
            await asyncio.sleep(2)  # the keepalive notices the takeover and cancels this body


def _sql(stmt: Any) -> str:  # noqa: ANN401
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))


def test_the_guard_checks_holder_fence_and_expiry_in_the_same_statement() -> None:  # inv. 10
    """The guard checks holder fence and expiry in the same statement."""
    lease = Lease(TENANT, "a", "k", "holder", 7)
    stmt = update(WebhookDelivery).where(WebhookDelivery.delivery_id == "x").values(status="dead")
    sql = _sql(stmt.where(lease.valid_clause()))
    assert sql.count("UPDATE") == 1  # one statement, not a check followed by a write
    for needle in ("EXISTS", "scope_leases.holder", "scope_leases.fence", "clock_timestamp()"):
        assert needle in sql
    assert "scope_leases.expires_at >" in sql


def test_guard_is_the_adapter_cores_name_for_the_same_predicate() -> None:
    """Guard is the adapter cores name for the same predicate."""
    lease = Lease(TENANT, "a", "k", "holder", 7)
    assert lease.fence == 7
    assert _sql(select(lease.guard())) == _sql(select(lease.valid_clause()))


class _Result:
    def __init__(self, rowcount: int) -> None:
        """Initialise."""
        self.rowcount = rowcount


class _Session:
    def __init__(self, rowcount: int) -> None:
        """Initialise."""
        self.rowcount = rowcount
        self.statements: list[str] = []

    async def execute(self, stmt: Any) -> _Result:  # noqa: ANN401
        """Execute."""
        self.statements.append(_sql(stmt))
        return _Result(self.rowcount)


async def test_a_guarded_write_that_matches_nothing_is_lease_lost() -> None:  # inv. 10
    """A guarded write that matches nothing is lease lost."""
    lease = Lease(TENANT, "a", "k", "holder", 7)
    stmt = update(WebhookDelivery).where(WebhookDelivery.host == "h").values(status="dead")
    lost = _Session(0)
    with pytest.raises(LeaseLost):
        await guarded_execute(lost, lease, stmt)  # type: ignore[arg-type]
    assert await guarded_execute(lost, lease, stmt, require_row=False) == 0  # type: ignore[arg-type]
    ok = _Session(1)
    assert await guarded_execute(ok, lease, stmt) == 1  # type: ignore[arg-type]
    assert "EXISTS" in ok.statements[0]


async def test_a_guarded_insert_selects_from_the_lease_check() -> None:  # inv. 10
    """A guarded insert selects from the lease check."""
    lease = Lease(TENANT, "a", "k", "holder", 7)
    table = WebhookDelivery.__table__
    session = _Session(1)
    await guarded_insert(session, lease, table, {"delivery_id": "x", "host": "h"})  # type: ignore[arg-type]
    sql = session.statements[0]
    assert sql.startswith("INSERT INTO webhook_deliveries")
    assert "SELECT" in sql
    assert "EXISTS" in sql
    with pytest.raises(LeaseLost):
        await guarded_insert(_Session(0), lease, table, {"delivery_id": "x"})  # type: ignore[arg-type]


async def test_an_adapter_call_is_bounded_by_its_timeout() -> None:  # inv. 11
    """An adapter call is bounded by its timeout."""
    async def slow() -> str:
        await asyncio.sleep(5)
        return "late"

    async def fast() -> str:
        return "ok"

    with pytest.raises(TimeoutError):
        await bounded_call(slow(), 0.05)
    assert await bounded_call(fast(), 1) == "ok"
