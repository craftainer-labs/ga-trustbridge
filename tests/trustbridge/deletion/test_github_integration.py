"""The GitHub step end to end against Postgres (NG6h-6, ADR 0034 step 3, B6 and B9).

Runs in CI's stack (integration). The real `SqlCredentialStore`, `SqlEnvironmentStore` and the
recorded rows, a fake writer, gateway and installation lookup, and `GithubDeletionStep` on top:
what the step lists comes from the rows only, outputs are cleared from their rows once removed,
and only tracked environments are ever deleted.
"""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from craftainer_crud.models.base import Base
from sqlalchemy import Table, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.db import build_engine
from tests.trustbridge.adapters.conftest import PULL, PUSH_PROD, World, build_world
from tests.trustbridge.deletion.fakes import record
from tests.trustbridge.worker.test_github_deletion import FakeGateway, FakeInstallations
from trustbridge.adapters import codec
from trustbridge.adapters.core import CredentialCore
from trustbridge.adapters.fake import fake_capabilities
from trustbridge.adapters.memory import RecordingWriter
from trustbridge.adapters.models import AdapterCredentialRow, CredentialOperationRow
from trustbridge.adapters.sql_store import SqlCredentialStore
from trustbridge.adapters.store import OutputError
from trustbridge.deletion.github import CREATED_AT_SKEW, GithubDeletionStep, GithubStepHeldError
from trustbridge.deletion.model import Lease, StepContext, StepOutcome
from trustbridge.github.client import ApiResponse, GitHubApiError
from trustbridge.github.environments import SqlEnvironmentStore
from trustbridge.github.gateway import Call
from trustbridge.github.models import GitHubEnvironmentRow
from trustbridge.worker.github_deletion import SqlGithubPort

pytestmark = pytest.mark.integration

TABLES: list[Table] = [
    cast("Table", m.__table__)
    for m in (AdapterCredentialRow, CredentialOperationRow, GitHubEnvironmentRow)
]
LONG_AGO = "2020-01-01T00:00:00Z"


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


class EnvGateway(FakeGateway):
    """GitHub's environments: `created_at` per name, and a record of the deletions."""

    def __init__(self) -> None:
        """Every environment is old unless a test says otherwise."""
        super().__init__()
        self.created: dict[str, str] = {}
        self.deleted: list[str] = []

    async def call(self, tenant: object, request: Call) -> ApiResponse:
        """Answer `GET` with the environment's time, record `DELETE`."""
        if request.method in self.errors:
            raise self.errors[request.method]
        name = request.path.rsplit("/", 1)[-1]
        if request.method == "DELETE":
            self.deleted.append(name)
            return ApiResponse(204, None)
        return ApiResponse(200, {"created_at": self.created.get(name, LONG_AGO)})


class Rig:
    """A world over the real stores, the port and the step."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], *, exists: bool = True) -> None:
        """Build the fake adapter world on Postgres rows."""
        self.sessions = sessions
        self.w: World = build_world(fake_capabilities(oidc=False))
        self.store = SqlCredentialStore(sessions, None)
        self.core = CredentialCore(replace(self.w.deps, store=self.store))
        self.writer = RecordingWriter()
        self.gateway = EnvGateway()
        self.installations = FakeInstallations(exists=exists)
        self.environments = SqlEnvironmentStore(sessions)
        self.port = SqlGithubPort(
            sessions=sessions,
            tenants=self.w.deps.tenants,
            writer=self.writer,
            gateway=cast("Any", self.gateway),
            environments=self.environments,
            installations=self.installations,
        )
        self.step = GithubDeletionStep(self.port)

    @property
    def tenant(self) -> UUID:
        """The tenant under deletion."""
        return self.w.tenant.id

    async def seed(self) -> None:
        """Two credential rows with outputs; `prod` is tracked, `legacy` is not."""
        await self.core.ensure_scope(self.w.tenant, "fake", PUSH_PROD)
        await self.core.ensure_scope(self.w.tenant, "fake", PULL)
        (prod,) = [
            row
            for row in await self.store.list_credentials(self.tenant, "fake")
            if row.scope.environment == "prod"
        ]
        env = {"kind": "environment", "repository_id": 10, "environment": None}
        async with self.sessions() as session, session.begin():
            await session.execute(
                update(AdapterCredentialRow)
                .where(
                    AdapterCredentialRow.tenant_id == self.tenant,
                    AdapterCredentialRow.scope_key == prod.scope_key,
                )
                .values(
                    outputs=[
                        *codec.outputs_to_json(prod.outputs),
                        {**env, "name": "prod"},
                        {**env, "name": "legacy"},
                    ]
                )
            )
        await self.environments.record_created(self.tenant, "github.com", 10, "prod")

    async def outputs(self) -> list[tuple[str, str]]:
        """`(kind, name)` of everything still recorded in the credential rows."""
        async with self.sessions() as session:
            rows = (await session.scalars(select(AdapterCredentialRow))).all()
        return sorted((str(o["kind"]), str(o["name"])) for row in rows for o in row.outputs)

    async def run(self, *, attempts: int = 0, cursor: dict[str, int] | None = None) -> StepOutcome:
        """One pass of the step."""
        deletion = uuid4()
        rec = record(
            id=deletion,
            tenant_id=self.tenant,
            state="github",
            step_cursor=cursor or {},
            attempts=attempts,
        )

        async def renew() -> None:
            return None

        return await self.step.run(StepContext(rec, Lease(deletion, "h", 1), renew))

    async def drain(self) -> list[StepOutcome]:
        """Passes the way the engine runs them, until `done`."""
        outcomes: list[StepOutcome] = []
        cursor: dict[str, int] = {}
        while True:
            out = await self.run(cursor=cursor)
            outcomes.append(out)
            if out.done:
                return outcomes
            cursor = dict(out.cursor or {})


async def _tracked(rig: Rig) -> list[tuple[int, str]]:
    return list(await rig.environments.tracked(rig.tenant))


async def test_the_step_removes_what_the_rows_record_and_only_tracked_environments(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Secrets and variables go and leave their rows; `prod` is deleted, `legacy` is kept."""
    rig = Rig(sessions)
    await rig.seed()
    before = await rig.port.outputs(rig.tenant)
    assert len(before) >= 2 and {o.kind for o in before} == {"secret", "variable"}
    assert await rig.port.untracked_environments(rig.tenant) == 1
    outcomes = await rig.drain()
    # the outputs scoped to `prod` go with the environment, not by name
    assert len(rig.writer.removed) == len([o for o in before if o.environment is None])
    assert any(o.environment == "prod" for o in before)
    assert rig.gateway.deleted == ["prod"]
    assert await _tracked(rig) == []
    # every secret, variable and the deleted environment left the rows; the untracked one stays
    assert await rig.outputs() == [("environment", "legacy")]
    total = {k: sum(o.counts.get(k, 0) for o in outcomes) for k in outcomes[0].counts}
    assert total == {
        "github_outputs_removed": len(before),
        "github_environments_removed": 1,
    }
    assert outcomes[-1].skipped == [
        {"category": "environment_kept", "reason": "untracked", "count": 1}
    ]


async def test_a_recreated_environment_is_kept_and_forgotten(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """GitHub's `created_at` later than the row's plus the margin: not deleted, recorded."""
    rig = Rig(sessions)
    await rig.seed()
    before = await rig.port.outputs(rig.tenant)
    scoped = len([o for o in before if o.environment == "prod"])
    assert scoped >= 1
    (_, _, row_time) = (await rig.environments.tracked_since(rig.tenant))[0]
    rig.gateway.created["prod"] = (row_time + CREATED_AT_SKEW + timedelta(seconds=5)).isoformat()
    outcomes = await rig.drain()
    assert rig.gateway.deleted == []
    assert await _tracked(rig) == []
    assert [s for o in outcomes for s in o.skipped if s["reason"] == "recreated"] == [
        {"category": "environment_kept", "reason": "recreated", "count": 1, "outputs": scoped}
    ]
    # the recreated environment's outputs are never removed by name
    assert len(rig.writer.removed) == len(before) - scoped
    assert await rig.outputs() == [("environment", "legacy")]


async def test_a_gone_installation_calls_nothing_and_leaves_the_rows_for_the_database_step(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """B9: ids and counts, no GitHub write, nothing cleared (step 5 deletes the rows)."""
    rig = Rig(sessions, exists=False)
    await rig.seed()
    before = await rig.outputs()
    out = await rig.run()
    assert out.done
    assert rig.writer.removed == [] and rig.gateway.calls == [] and rig.gateway.deleted == []
    assert rig.installations.asked == [("github.com", 42)]
    assert {(s["category"], s["reason"]) for s in out.skipped} == {
        ("github_outputs", "installation_gone"),
        ("github_environments", "installation_gone"),
        ("environment_kept", "untracked"),
    }
    assert all(
        isinstance(s.get("repository_ids"), list)
        for s in out.skipped
        if s["reason"] == "installation_gone"
    )
    assert await rig.outputs() == before
    assert await _tracked(rig) == [(10, "prod")]


async def test_one_failing_removal_does_not_hold_the_rest_and_the_step_converges(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A failed writer call leaves that output recorded; the next pass removes it."""
    rig = Rig(sessions)
    await rig.seed()
    total = len(await rig.port.outputs(rig.tenant))
    rig.writer.fail_remove = [OutputError("boom", retryable=True)]
    first = await rig.run()
    assert first.counts["github_outputs_removed"] == total - 1
    assert len(await rig.port.outputs(rig.tenant)) == 1
    second = await rig.run(attempts=0, cursor=dict(first.cursor or {}))
    assert second.counts == {"github_outputs_removed": 1}
    assert await rig.port.outputs(rig.tenant) == []


async def test_a_pass_where_everything_fails_holds(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Only failures: the step holds, and the rows are untouched."""
    rig = Rig(sessions)
    await rig.seed()
    outputs = await rig.port.outputs(rig.tenant)
    rig.writer.fail_remove = [OutputError("boom") for _ in range(len(outputs) + 1)]
    rig.gateway.errors["GET"] = GitHubApiError(503, "down")
    with pytest.raises(GithubStepHeldError):
        await rig.run()
    assert len(await rig.port.outputs(rig.tenant)) == len(outputs)
    assert await _tracked(rig) == [(10, "prod")]


async def test_another_tenants_rows_are_not_listed(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The listings are keyed on the deleted tenant."""
    rig = Rig(sessions)
    await rig.seed()
    other = uuid4()
    assert await rig.port.outputs(other) == []
    assert await rig.port.environments(other) == []
    assert await rig.port.untracked_environments(other) == 0


async def test_releasing_an_environment_twice_is_harmless(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A crash between the two release writes repeats cleanly."""
    rig = Rig(sessions)
    await rig.seed()
    (env,) = await rig.port.environments(rig.tenant)
    await rig.port.release_environment(rig.tenant, env)
    await rig.port.release_environment(rig.tenant, env)
    assert await _tracked(rig) == []
    assert ("environment", "prod") not in await rig.outputs()


async def test_a_vanished_credential_row_is_not_an_error(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Clearing an entry from a row that is already gone does nothing."""
    rig = Rig(sessions)
    await rig.seed()
    (first, *_) = await rig.port.outputs(rig.tenant)
    async with sessions() as session, session.begin():
        await session.execute(AdapterCredentialRow.__table__.delete())  # type: ignore[attr-defined]
    await rig.port.remove_output(rig.tenant, first)
    assert len(rig.writer.removed) == 1


async def test_an_output_whose_repository_is_gone_is_cleared(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A 404 from the repository lookup means nothing is left to remove: the entry is cleared."""
    rig = Rig(sessions)
    await rig.seed()
    (first, *rest) = await rig.port.outputs(rig.tenant)
    gone = OutputError("GitHub delete failed", retryable=False)
    gone.__cause__ = GitHubApiError(404, "Not Found")
    rig.writer.fail_remove = [gone]
    assert await rig.port.remove_output(rig.tenant, first) is False
    assert await rig.port.outputs(rig.tenant) == rest


async def test_any_other_writer_error_leaves_the_output_recorded(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Only a 404 is read as gone; a 5xx or a refusal keeps the entry for the next pass."""
    rig = Rig(sessions)
    await rig.seed()
    before = await rig.port.outputs(rig.tenant)
    failing = OutputError("GitHub delete failed", retryable=True)
    failing.__cause__ = GitHubApiError(503, "down")
    rig.writer.fail_remove = [failing]
    with pytest.raises(OutputError):
        await rig.port.remove_output(rig.tenant, before[0])
    assert await rig.port.outputs(rig.tenant) == before


async def test_releasing_an_environment_clears_the_outputs_scoped_to_it(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Its secrets go with it (or are a person's after a recreation): never removed by name."""
    rig = Rig(sessions)
    await rig.seed()
    scoped = {"kind": "secret", "name": "FOO", "repository_id": 10, "environment": "prod"}
    async with sessions() as session, session.begin():
        row = (await session.scalars(select(AdapterCredentialRow))).first()
        assert row is not None
        row.outputs = [*row.outputs, scoped]
    (env,) = await rig.port.environments(rig.tenant)
    await rig.port.release_environment(rig.tenant, env)
    assert ("secret", "FOO") not in await rig.outputs()
    assert ("environment", "legacy") in await rig.outputs()
