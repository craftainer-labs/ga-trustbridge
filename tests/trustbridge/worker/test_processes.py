"""The worker/reconciler commands and the migrate Job (0028 invariants 15, 17, 18).

The database-backed parts of the migrate Job are in `test_integration_pg.py`.
"""

import asyncio
import sys
import types
from typing import Any

import pytest

from tests.trustbridge.fakes import FakeHostChecker, FakeRedis
from tests.trustbridge.helpers import as_redis, make_settings
from trustbridge.worker import migrate, runtime
from trustbridge.worker.hosts import HostState, UninstalledHostChecker
from trustbridge.worker.runtime import (
    HostRuntime,
    ProcessOverrides,
    build_core,
    load_host_runtime,
)
from trustbridge.worker.schema import REQUIRED_SCHEMA_VERSION


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
    kind: str, no_migrations: None
) -> None:  # inv. 15 and 17
    stop = asyncio.Event()
    stop.set()
    checker = FakeHostChecker()
    overrides = ProcessOverrides(
        redis=as_redis(FakeRedis()), runtime=HostRuntime(checker=checker), stop=stop
    )
    code = await runtime.run_process(kind, make_settings(), overrides)  # type: ignore[arg-type]
    assert code == 0
    assert checker.calls == ["github.com"]  # the startup checks ran, as in the api


async def test_the_process_exits_non_zero_when_the_schema_never_becomes_current(
    no_migrations: None,
) -> None:  # inv. 14
    class Session:
        async def __aenter__(self) -> "Session":
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
            session_factory=lambda: Session(),  # type: ignore[arg-type,return-value]
            runtime=HostRuntime(checker=FakeHostChecker()),
            stop=asyncio.Event(),
        ),
    )
    assert code == 1


def test_without_the_github_runtime_module_every_registered_host_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(name: str) -> None:
        raise ModuleNotFoundError(name=name)

    monkeypatch.setattr(runtime.importlib, "import_module", missing)
    loaded = load_host_runtime(make_settings())
    assert isinstance(loaded.checker, UninstalledHostChecker)


def test_the_github_runtime_hook_is_called_when_the_module_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = HostRuntime(checker=FakeHostChecker())
    hook = types.ModuleType("trustbridge.github.runtime")
    hook.build_host_runtime = lambda settings: sentinel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "trustbridge.github.runtime", hook)
    assert load_host_runtime(make_settings()) is sentinel


def test_a_broken_hook_module_is_not_mistaken_for_a_missing_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(name: str) -> None:
        raise ModuleNotFoundError(name="some_third_party")

    monkeypatch.setattr(runtime.importlib, "import_module", broken)
    with pytest.raises(ModuleNotFoundError):
        load_host_runtime(make_settings())


async def test_build_core_uses_in_memory_stores_in_mock_mode() -> None:  # inv. 15
    core = build_core(make_settings(), redis=None, session_factory=None, runtime=HostRuntime(FakeHostChecker()))
    assert core.queue is None
    assert core.relay is None
    assert core.gate.current
    await core.hosts.check_all()
    assert core.hosts.status("github.com").state is HostState.READY  # type: ignore[union-attr]


# --- the migrate Job -------------------------------------------------------------------------


class _Conn:
    def __init__(self, log: list[str], role: str) -> None:
        self.log, self.role = log, role

    async def __aenter__(self) -> "_Conn":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, stmt: Any, params: Any = None) -> Any:  # noqa: ANN401
        text = str(stmt)
        self.log.append(text)
        return types.SimpleNamespace(scalar_one=lambda: self.role)


class _Engine:
    def __init__(self, log: list[str], role: str) -> None:
        self.log, self.role = log, role

    def connect(self) -> _Conn:
        return _Conn(self.log, self.role)

    async def dispose(self) -> None:
        self.log.append("dispose")


def _patch_engine(monkeypatch: pytest.MonkeyPatch, log: list[str], role: str = "owner") -> None:
    monkeypatch.setattr(migrate, "create_async_engine", lambda *_a, **_k: _Engine(log, role))


async def test_migrate_runs_upgrade_under_the_lock_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # inv. 18
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
    log: list[str] = []
    _patch_engine(monkeypatch, log)

    def failing() -> None:
        raise RuntimeError("DDL failed")

    assert await migrate.run_upgrade(make_settings(), upgrade=failing) == 1
    assert "SELECT pg_advisory_unlock(:k)" in log


async def test_the_job_refuses_to_run_as_any_role_but_the_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # inv. 18
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
    assert migrate.main([]) == 2
    assert migrate.main(["downgrade"]) == 2
    called: list[list[str]] = []
    monkeypatch.setattr(migrate, "alembic_main", lambda argv: called.append(argv))
    assert migrate.main(["revision", "--autogenerate", "-m", "x"]) == 0
    assert called == [["revision", "--autogenerate", "-m", "x"]]
    from trustbridge.worker import models  # noqa: PLC0415

    assert "webhook_deliveries" in models.Base.metadata.tables  # registered for autogenerate
