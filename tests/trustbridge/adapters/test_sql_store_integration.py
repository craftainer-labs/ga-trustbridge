"""SQL store against the real Postgres service (integration; run by CI, not by the unit lane)."""

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import cast
from uuid import UUID, uuid4

import pytest
from craftainer_crud.models.base import Base
from sqlalchemy import ColumnElement, Table, false, func, select, true
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.db import build_engine
from tests.trustbridge.adapters.conftest import PUSH_PROD, build_world
from trustbridge.adapters.core import CredentialCore
from trustbridge.adapters.fake import fake_capabilities
from trustbridge.adapters.interface import CredentialSlot, CredentialState, ResourceState
from trustbridge.adapters.models import (
    AdapterCredentialRow,
    AdapterResourceRow,
    CredentialOperationRow,
)
from trustbridge.adapters.sql_store import SqlCredentialStore
from trustbridge.adapters.store import (
    LeaseLostError,
    Operation,
    OperationKind,
    OperationStatus,
    StoredCredential,
)
from trustbridge.github.models import HostRegistrationRow, Registration, SqlRegistrationStore

pytestmark = pytest.mark.integration

TABLES: list[Table] = [
    cast("Table", m.__table__)
    for m in (AdapterResourceRow, AdapterCredentialRow, CredentialOperationRow, HostRegistrationRow)
]


class GuardLease:
    """A lease whose guard is a constant, to test both branches of the fencing predicate."""

    def __init__(self, *, live: bool) -> None:
        """Choose whether the guard is true."""
        self.fence = 1
        self._live = live

    def guard(self) -> ColumnElement[bool]:
        """A SQL predicate that is constant."""
        return true() if self._live else false()


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """An engine with the NG4 tables present and empty (created here when not migrated)."""
    eng = build_engine(Settings())
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=TABLES, checkfirst=True)
        for table in TABLES:
            await conn.execute(table.delete())
    yield eng
    async with eng.begin() as conn:
        for table in TABLES:
            await conn.execute(table.delete())
    await eng.dispose()


def make_store(engine: AsyncEngine) -> SqlCredentialStore:
    """A store using short-lived sessions on `engine`."""
    factory = async_sessionmaker(engine, expire_on_commit=False)

    def session() -> AsyncSession:
        return factory()

    return SqlCredentialStore(session)


def stored(tenant_id: UUID, state: CredentialState) -> StoredCredential:
    """A credential row for the test scope."""
    return StoredCredential(
        tenant_id=tenant_id,
        adapter="fake",
        scope_key="k1",
        scope=PUSH_PROD,
        kind="secret",
        state=state,
    )


def operation(tenant_id: UUID, status: OperationStatus) -> Operation:
    """An operation row for the test scope."""
    return Operation(
        id=uuid4(),
        tenant_id=tenant_id,
        adapter="fake",
        scope_key="k1",
        kind=OperationKind.ISSUE,
        status=status,
        oidc_offered=False,
        base_state=None,
        base_kind=None,
    )


async def test_credential_and_operation_roundtrip(engine: AsyncEngine) -> None:
    """A guarded save writes both rows; reads decode them."""
    store, tenant = make_store(engine), uuid4()
    state = CredentialState(CredentialSlot(1, "n1", {"id": "7"}), None)
    op = operation(tenant, OperationStatus.INTENT)
    await store.save(GuardLease(live=True), stored(tenant, state), op)
    assert (await store.get_credential(tenant, "fake", "k1")) == stored(tenant, state)
    assert await store.get_open_operation(tenant, "fake", "k1") == op
    assert await store.count_slots(tenant, "fake") == 1
    assert await store.known_names(tenant, "fake") == {"n1"}


async def test_a_dead_lease_writes_nothing(engine: AsyncEngine) -> None:
    """Invariant 10: the guard is in the write statement, so a lost lease commits zero rows."""
    store, tenant = make_store(engine), uuid4()
    state = CredentialState(CredentialSlot(1, "n1"), None)
    with pytest.raises(LeaseLostError):
        await store.save(
            GuardLease(live=False), stored(tenant, state), operation(tenant, OperationStatus.INTENT)
        )
    assert await store.get_credential(tenant, "fake", "k1") is None
    with pytest.raises(LeaseLostError):
        await store.put_resource(
            GuardLease(live=False), tenant, "fake", "project:p", ResourceState("n")
        )


async def test_one_open_operation_per_scope(engine: AsyncEngine) -> None:
    """The partial unique index admits a second row only once the first is terminal."""
    store, tenant = make_store(engine), uuid4()
    lease = GuardLease(live=True)
    state = CredentialState(CredentialSlot(1, "n1"), None)
    first = operation(tenant, OperationStatus.INTENT)
    await store.save(lease, stored(tenant, state), first)
    with pytest.raises(IntegrityError):
        await store.save(lease, stored(tenant, state), operation(tenant, OperationStatus.INTENT))
    finished = Operation(
        id=first.id,
        tenant_id=first.tenant_id,
        adapter=first.adapter,
        scope_key=first.scope_key,
        kind=first.kind,
        status=OperationStatus.FINISHED,
        oidc_offered=False,
        base_state=None,
        base_kind=None,
    )
    await store.save(lease, stored(tenant, state), finished)
    await store.save(lease, stored(tenant, state), operation(tenant, OperationStatus.INTENT))
    assert len(await store.list_open_operations()) == 1


async def test_resources_roundtrip_and_forget(engine: AsyncEngine) -> None:
    """Non-secret resource state is stored and removed under the lease."""
    store, tenant, lease = make_store(engine), uuid4(), GuardLease(live=True)
    await store.put_resource(lease, tenant, "fake", "project:p", ResourceState("n", {"id": "1"}))
    assert await store.get_resource(tenant, "fake", "project:p") == ResourceState("n", {"id": "1"})
    await store.put_resource(lease, tenant, "fake", "project:p", None)
    assert await store.get_resource(tenant, "fake", "project:p") is None


async def test_core_issues_a_credential_through_the_sql_store(engine: AsyncEngine) -> None:
    """End to end: the core's write-ahead flow against Postgres, with the fake adapter."""
    world = build_world(fake_capabilities(oidc=False))
    store = make_store(engine)
    core = CredentialCore(replace(world.deps, store=store))
    result = await core.ensure_scope(world.tenant, "fake", PUSH_PROD)
    assert result.status is OperationStatus.FINISHED
    rows = await store.list_credentials(world.tenant.id, "fake")
    assert [r.kind for r in rows] == ["secret"]
    assert await store.list_open_operations() == []


async def test_host_registration_store_upserts(engine: AsyncEngine) -> None:
    """Every process can read what the registration flow saved."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    regs = SqlRegistrationStore(factory)
    await regs.save("ghes", Registration(5, "ref/a", "slug"))
    await regs.save("ghes", Registration(6, "ref/b", None))
    assert await regs.load_all() == {"ghes": Registration(6, "ref/b", None)}


class CountGuardLease:
    """A guard that is true only while the tenant has no credential row yet."""

    def __init__(self, tenant_id: UUID) -> None:
        """Remember the tenant whose credential rows are counted."""
        self.fence = 1
        self._tenant = tenant_id

    def guard(self) -> ColumnElement[bool]:
        """True for the credential upsert, false for the operation upsert that follows."""
        table = AdapterCredentialRow.__table__
        count = select(func.count()).select_from(table).where(table.c.tenant_id == self._tenant)
        return count.scalar_subquery() == 0


async def test_operations_are_read_by_id_and_a_credential_can_be_saved_alone(
    engine: AsyncEngine,
) -> None:
    """`get_operation` finds or misses; an operation is optional in a save."""
    store, tenant, lease = make_store(engine), uuid4(), GuardLease(live=True)
    state = CredentialState(CredentialSlot(1, "n1"), None)
    op = operation(tenant, OperationStatus.INTENT)
    await store.save(lease, stored(tenant, state), None)
    assert await store.get_operation(op.id) is None
    await store.save(lease, stored(tenant, state), op)
    assert await store.get_operation(op.id) == op


async def test_a_guard_that_fails_between_the_two_upserts_rolls_everything_back(
    engine: AsyncEngine,
) -> None:
    """If the lease is lost after the credential row, the operation write fails and nothing stays."""
    store, tenant = make_store(engine), uuid4()
    state = CredentialState(CredentialSlot(1, "n1"), None)
    with pytest.raises(LeaseLostError):
        await store.save(
            CountGuardLease(tenant), stored(tenant, state), operation(tenant, OperationStatus.INTENT)
        )
    assert await store.get_credential(tenant, "fake", "k1") is None
