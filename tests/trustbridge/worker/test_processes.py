"""The worker/reconciler commands and the migrate Job (0028 invariants 15, 17, 18).

The database-backed parts of the migrate Job are in `test_integration_pg.py`.
"""

import asyncio
import types
from datetime import UTC, datetime
from typing import Any

import pytest
from craftainer_crud.models.base import Base

from tests.trustbridge.fakes import FakeHostChecker, FakeRedis
from tests.trustbridge.helpers import as_redis, make_settings
from trustbridge.github.availability import HostState
from trustbridge.worker import migrate, runtime
from trustbridge.worker.deliveries import NewDelivery
from trustbridge.worker.runtime import ProcessOverrides, build_core
from trustbridge.worker.schema import REQUIRED_SCHEMA_VERSION
from trustbridge.worker.secrets import MemorySecrets


@pytest.fixture
def no_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any attempt by a runtime process to run Alembic fails the test."""

    def refuse(*_a: object, **_k: object) -> None:
        raise AssertionError("a runtime process executed a migration")

    monkeypatch.setattr("alembic.command.upgrade", refuse)
    monkeypatch.setattr("alembic.command.stamp", refuse)
    monkeypatch.setattr("trustbridge.worker.migrate._alembic_upgrade", refuse)


@pytest.mark.parametrize("kind", ["worker", "reconciler"])
async def test_runtime_processes_start_and_stop_without_migrating_or_touching_a_db(
    kind: str, no_migrations: None, key_signers: None
) -> None:  # inv. 15 and 17
    """Runtime processes start and stop without migrating or touching a db."""
    stop = asyncio.Event()
    stop.set()
    checker = FakeHostChecker()
    overrides = ProcessOverrides(redis=as_redis(FakeRedis()), checker=checker, stop=stop)
    code = await runtime.run_process(kind, make_settings(), overrides)  # type: ignore[arg-type]
    assert code == 0
    assert checker.calls == ["github.com"]  # the startup checks ran, as in the api


async def test_the_process_exits_non_zero_when_the_schema_never_becomes_current(
    no_migrations: None, key_signers: None
) -> None:  # inv. 14
    """The process exits non zero when the schema never becomes current."""
    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def execute(self, _stmt: object) -> Any:  # noqa: ANN401
            return types.SimpleNamespace(scalar_one_or_none=lambda: REQUIRED_SCHEMA_VERSION - 1)

    settings = make_settings(mode="dev", allow_mock_mode=False, schema_wait_seconds=0.05, schema_poll_seconds=0.01)
    code = await runtime.run_process(
        "worker",
        settings,
        ProcessOverrides(
            redis=as_redis(FakeRedis()),
            session_factory=Session,  # type: ignore[arg-type]
            checker=FakeHostChecker(),
            stop=asyncio.Event(),
        ),
    )
    assert code == 1


async def test_build_core_uses_in_memory_stores_in_mock_mode(key_signers: None) -> None:  # inv. 15
    """Build core uses in memory stores in mock mode."""
    core = build_core(make_settings(), redis=None, session_factory=None, checker=FakeHostChecker())
    assert core.queue is None
    assert core.relay is None
    assert core.gate.current
    assert isinstance(core.secrets, MemorySecrets)
    monitor = core.runtime.monitor
    await monitor.check_all()
    assert monitor.status("github.com").state is HostState.READY
    tenant_id = await core.tenants.upsert_installation("github.com", 7, "acme", 1)
    assert (await core.tenants.get_tenant(tenant_id)).github_host.name == "github.com"
    assert core.runtime.argocd is None  # until NG5d supplies a DeploymentStateSource


async def test_a_worker_re_adds_the_waiting_rows_of_a_host_that_turns_ready(
    key_signers: None,
) -> None:  # ADR 0030 invariant 8
    """A worker re adds the waiting rows of a host that turns ready."""
    valkey = FakeRedis()
    core = build_core(
        make_settings(),
        redis=as_redis(valkey),
        session_factory=None,
        readd_on_ready=True,
        checker=FakeHostChecker(),
    )
    await core.store.insert(NewDelivery("d-1", "github.com", "push", {}, b"{}", datetime.now(UTC)))
    assert valkey.ids_of("tb:events") == []
    await core.runtime.monitor.check_all()
    assert valkey.ids_of("tb:events") == ["d-1"]


async def test_the_readd_hook_does_nothing_without_a_stream(key_signers: None) -> None:
    """The readd hook does nothing without a stream."""
    core = build_core(
        make_settings(),
        redis=None,
        session_factory=None,
        readd_on_ready=True,
        checker=FakeHostChecker(),
    )
    await core.runtime.monitor.check_all()
    assert core.runtime.monitor.is_ready("github.com")


# --- the migrate Job -------------------------------------------------------------------------


class _Conn:
    def __init__(self, log: list[str], role: str) -> None:
        """Initialise."""
        self.log, self.role = log, role

    async def __aenter__(self) -> _Conn:
        """Implement __aenter__."""
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Implement __aexit__."""
        return None

    async def execute(self, stmt: Any, params: Any = None) -> Any:  # noqa: ANN401
        """Execute."""
        text = str(stmt)
        self.log.append(text)
        return types.SimpleNamespace(scalar_one=lambda: self.role)


class _Engine:
    def __init__(self, log: list[str], role: str) -> None:
        """Initialise."""
        self.log, self.role = log, role

    def connect(self) -> _Conn:
        """Connect."""
        return _Conn(self.log, self.role)

    async def dispose(self) -> None:
        """Dispose."""
        self.log.append("dispose")


def _patch_engine(monkeypatch: pytest.MonkeyPatch, log: list[str], role: str = "owner") -> None:
    monkeypatch.setattr(migrate, "create_async_engine", lambda *_a, **_k: _Engine(log, role))


async def test_migrate_runs_upgrade_under_the_lock_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # inv. 18
    """Migrate runs upgrade under the lock and exits zero."""
    log: list[str] = []
    _patch_engine(monkeypatch, log)
    result = await migrate.run_upgrade(make_settings(), upgrade=lambda: log.append("upgrade"))
    assert result == 0
    lock, upgrade, unlock = (log.index(x) for x in ("SELECT pg_advisory_lock(:k)", "upgrade", "SELECT pg_advisory_unlock(:k)"))
    assert lock < upgrade < unlock  # DDL happens only while the lock is held
    assert log[-1] == "dispose"


async def test_a_failed_migration_fails_the_job_and_still_unlocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # inv. 18
    """A failed migration fails the job and still unlocks."""
    log: list[str] = []
    _patch_engine(monkeypatch, log)

    def failing() -> None:
        raise RuntimeError("DDL failed")

    assert await migrate.run_upgrade(make_settings(), upgrade=failing) == 1
    assert "SELECT pg_advisory_unlock(:k)" in log


async def test_the_job_refuses_to_run_as_any_role_but_the_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # inv. 18
    """The job refuses to run as any role but the owner."""
    log: list[str] = []
    _patch_engine(monkeypatch, log, role="trustbridge_app")
    ran: list[str] = []
    settings = make_settings(migrate_expected_role="trustbridge_owner")
    assert await migrate.run_upgrade(settings, upgrade=lambda: ran.append("x")) == 1
    assert ran == []
    assert not any("advisory_lock" in line for line in log)


def test_migrate_cli_rejects_unknown_commands_and_wraps_autogenerate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Migrate cli rejects unknown commands and wraps autogenerate."""
    assert migrate.main([]) == 2
    assert migrate.main(["downgrade"]) == 2
    called: list[list[str]] = []

    def fake_alembic(argv: list[str]) -> None:
        called.append(argv)

    monkeypatch.setattr(migrate, "alembic_main", fake_alembic)
    assert migrate.main(["revision", "--autogenerate", "-m", "x"]) == 0
    assert called == [["revision", "--autogenerate", "-m", "x"]]
    # webhook_deliveries (NG3), adapter_credentials and github_host_registrations (NG4)
    tables = Base.metadata.tables
    assert {"webhook_deliveries", "adapter_credentials", "github_host_registrations"} <= set(tables)
