"""The platform step end to end against Postgres (NG6h-5, ADR 0034 step 2).

Runs in CI's stack (integration). The real `SqlCredentialStore` and the recorded rows, the real
cores over the fake adapter and platform, and `AdapterPlatformStep` on top: what the step lists
comes from the rows only, and it ends with nothing recorded as a credential or resource.
"""

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import cast
from uuid import UUID, uuid4

import pytest
from craftainer_crud.models.base import Base
from sqlalchemy import Table, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.db import build_engine
from tests.trustbridge.adapters.conftest import PULL, PUSH_PROD, World, build_world
from tests.trustbridge.deletion.fakes import record
from trustbridge.adapters.core import CredentialCore
from trustbridge.adapters.fake import fake_capabilities
from trustbridge.adapters.interface import ProjectSpec, RepoSpec
from trustbridge.adapters.models import (
    AdapterCredentialRow,
    AdapterResourceRow,
    CredentialOperationRow,
)
from trustbridge.adapters.resources import ResourceCore
from trustbridge.adapters.sql_store import SqlCredentialStore
from trustbridge.deletion.model import Lease, StepContext, StepOutcome
from trustbridge.deletion.platform import AdapterPlatformStep
from trustbridge.worker.platform_deletion import (
    ConnectionNames,
    SqlPlatformPort,
    UnknownResourceKindError,
)

pytestmark = pytest.mark.integration

TABLES: list[Table] = [
    cast("Table", m.__table__)
    for m in (AdapterResourceRow, AdapterCredentialRow, CredentialOperationRow)
]


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A session factory over tables that are present and empty."""
    engine: AsyncEngine = build_engine(Settings())
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=TABLES, checkfirst=True)
        for table in TABLES:
            await conn.execute(table.delete())
    yield async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        for table in TABLES:
            await conn.execute(table.delete())
    await engine.dispose()


class _Refs:
    async def list_refs(self, prefix: str) -> list[str]:
        return [f"store:{prefix}argocd"]


def _build(
    sessions: async_sessionmaker[AsyncSession],
) -> tuple[World, SqlCredentialStore, SqlPlatformPort, AdapterPlatformStep]:
    w = build_world(fake_capabilities(oidc=False))
    store = SqlCredentialStore(sessions, None)
    deps = replace(w.deps, store=store)
    port = SqlPlatformPort(
        sessions, store, w.deps.tenants, CredentialCore(deps), ResourceCore(deps)
    )
    return w, store, port, AdapterPlatformStep(port, ConnectionNames(_Refs()))


async def _run(step: AdapterPlatformStep, tenant: UUID, *, flag: bool) -> list[StepOutcome]:
    outcomes: list[StepOutcome] = []
    cursor: dict[str, int] = {}
    deletion = uuid4()
    while True:
        rec = record(
            id=deletion,
            tenant_id=tenant,
            state="platform",
            delete_platform_resources=flag,
            step_cursor=cursor,
        )

        async def renew() -> None:
            return None

        out = await step.run(StepContext(rec, Lease(deletion, "h", 1), renew))
        outcomes.append(out)
        if out.done:
            return outcomes
        cursor = dict(out.cursor or {})


async def test_the_step_removes_what_the_rows_record_and_nothing_else(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Credentials and webhooks always; repositories and projects only with the flag."""
    w, store, port, step = _build(sessions)
    deps = replace(w.deps, store=store)
    core = CredentialCore(deps)
    await core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    await core.ensure_scope(w.tenant, "fake", PULL)
    res = ResourceCore(deps)
    await res.ensure_project(w.tenant, "fake", ProjectSpec("p1"))
    await res.ensure_repository(w.tenant, "fake", "p1", RepoSpec("r1"))
    before = [(i.kind, i.adapter) for i in await port.pending(w.tenant.id)]
    assert before == [
        ("credential", "fake"),
        ("credential", "fake"),
        ("project", "fake"),
        ("repository", "fake"),
    ]
    outcomes = await _run(step, w.tenant.id, flag=False)
    assert sum(o.counts.get("platform_access_grants", 0) for o in outcomes) == 2
    assert [(i.kind) for i in await port.pending(w.tenant.id)] == ["project", "repository"]
    assert w.platform.credentials == {}
    assert len(w.platform.resources) == 2
    assert w.writer.removed == []
    assert outcomes[-1].skipped[-1] == {
        "category": "operator_owned_connections",
        "reason": "operator_owned",
        "adapter": "argocd",
        "count": 1,
    }
    again = await _run(step, w.tenant.id, flag=True)
    assert sum(o.counts.get("platform_projects", 0) for o in again) == 1
    assert await port.pending(w.tenant.id) == []
    assert w.platform.resources == {}
    kept = await store.list_credentials(w.tenant.id, "fake")
    assert all(row.state.current is None and row.outputs for row in kept)


async def test_another_tenants_rows_are_not_listed(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The listing is keyed on the deleted tenant."""
    w, store, port, _ = _build(sessions)
    await CredentialCore(replace(w.deps, store=store)).ensure_scope(w.tenant, "fake", PULL)
    assert await port.pending(uuid4()) == []
    assert len(await port.pending(w.tenant.id)) == 1


async def test_an_unrecognised_resource_key_holds_the_step(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A row the step cannot classify is an error (retry, alert), not something it skips."""
    w, _, port, _ = _build(sessions)
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO adapter_resources (tenant_id, adapter, resource_key, state) "
                "VALUES (:t, 'fake', 'mystery:x', CAST('{}' AS jsonb))"
            ),
            {"t": w.tenant.id},
        )
    with pytest.raises(UnknownResourceKindError):
        await port.pending(w.tenant.id)
