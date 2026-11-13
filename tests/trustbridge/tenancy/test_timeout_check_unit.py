"""The transaction time bound's hook and check logic, without a database.

ADR 0032 "Export" (lag-bounded) and ADR 0034 B8. The Postgres-backed half is in
`test_timeout_check_integration.py`.
"""

import asyncio
import logging
import types
from types import SimpleNamespace
from typing import Any

import pytest
from craftainer_observability.health.registry import HealthRegistry

from tests.trustbridge.fakes import FakeHostChecker, FakeRedis
from tests.trustbridge.helpers import as_redis, make_settings
from trustbridge import main as main_module
from trustbridge.tenancy import (
    RoleGate,
    TimeoutCheck,
    TimeoutFacts,
    TimeoutGate,
    configure_transaction_timeout,
    evaluate_timeouts,
    register_timeout_check,
)
from trustbridge.tenancy import hook as hook_module
from trustbridge.tenancy import timeout_check as tc
from trustbridge.worker import runtime
from trustbridge.worker.runtime import ProcessOverrides
from trustbridge.worker.schema import SchemaGate

LAG = 30.0


def _facts(
    transaction: int | None = 15_000, statement: int = 0, idle: int = 0, version: int = 170002
) -> TimeoutFacts:
    return TimeoutFacts(version, transaction, statement, idle)


class _Conn:
    """A stand-in connection that records what the hook executes."""

    def __init__(self, dialect: str, version: tuple[int, ...] | None = (17, 0)) -> None:
        self.dialect = SimpleNamespace(name=dialect, server_version_info=version)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def execute(self, stmt: object, params: dict[str, str]) -> None:
        self.calls.append((str(stmt), params))


@pytest.fixture(autouse=True)
def _restore_bound() -> Any:  # noqa: ANN401
    saved = hook_module._timeout_ms
    yield
    hook_module._timeout_ms = saved


# --- the hook ------------------------------------------------------------------------------


def test_the_hook_sets_transaction_timeout_on_postgres_17_and_later() -> None:
    """PostgreSQL 17+: one `set_config('transaction_timeout', ..., true)` in whole milliseconds."""
    configure_transaction_timeout(15)
    conn = _Conn("postgresql", (17, 2))
    hook_module._set_transaction_timeout(None, None, conn)  # type: ignore[arg-type]
    [(sql, params)] = conn.calls
    assert "'transaction_timeout'" in sql
    assert sql.rstrip().endswith("true)")  # transaction-local: SET LOCAL
    assert params == {"ms": "15000ms"}


def test_the_hook_falls_back_to_statement_and_idle_timeouts_before_17() -> None:
    """PostgreSQL 16 (or an unknown version) has no transaction_timeout: the fallback pair."""
    configure_transaction_timeout(0.0004)  # rounds up to 1 ms, never 0 (off)
    for version in ((16, 4), None):
        conn = _Conn("postgresql", version)
        hook_module._set_transaction_timeout(None, None, conn)  # type: ignore[arg-type]
        [(sql, params)] = conn.calls
        assert "transaction_timeout" not in sql
        assert "'statement_timeout'" in sql
        assert "'idle_in_transaction_session_timeout'" in sql
        assert params == {"ms": "1ms"}


def test_the_hook_does_nothing_without_a_bound_or_off_postgres() -> None:
    """No configured bound, or another dialect: nothing is executed."""
    configure_transaction_timeout(None)
    conn = _Conn("postgresql")
    hook_module._set_transaction_timeout(None, None, conn)  # type: ignore[arg-type]
    configure_transaction_timeout(5)
    other = _Conn("sqlite")
    hook_module._set_transaction_timeout(None, None, other)  # type: ignore[arg-type]
    assert conn.calls == [] == other.calls


def test_installing_the_hook_registers_the_timeout_listener_once_and_configures_it() -> None:
    """`install_tenant_hook(seconds)` sets the bound; a call without one keeps it."""
    from sqlalchemy import event  # noqa: PLC0415
    from sqlalchemy.orm import Session  # noqa: PLC0415

    hook_module.install_tenant_hook(12)
    hook_module.install_tenant_hook()
    assert hook_module._timeout_ms == 12_000
    assert event.contains(Session, "after_begin", hook_module._set_transaction_timeout)


# --- the decision --------------------------------------------------------------------------


def test_a_transaction_timeout_below_the_lag_is_safe() -> None:
    """17+: 0 < transaction_timeout < lag passes, with no warning."""
    verdict = evaluate_timeouts(_facts(15_000), LAG)
    assert verdict.ok
    assert verdict.reason is None
    assert verdict.warning is None


@pytest.mark.parametrize("value", [0, 30_000, 45_000])
def test_a_transaction_timeout_that_is_off_or_not_below_the_lag_is_unsafe(value: int) -> None:
    """17+: 0 (off), equal to the lag and above it all fail, naming the lag."""
    verdict = evaluate_timeouts(_facts(value), LAG)
    assert not verdict.ok
    assert verdict.reason is not None
    assert "AUDIT_EXPORT_LAG_SECONDS" in verdict.reason
    assert f"{value} ms" in verdict.reason


def test_before_17_the_statement_and_idle_fallback_passes_with_a_warning() -> None:
    """16: both timeouts set below the lag is the documented fallback; it warns."""
    verdict = evaluate_timeouts(_facts(None, 15_000, 15_000, 160004), LAG)
    assert verdict.ok
    assert verdict.warning is not None
    assert "160004" in verdict.warning
    assert "PostgreSQL 17" in verdict.warning


@pytest.mark.parametrize(("statement", "idle"), [(0, 15_000), (15_000, 0), (30_000, 15_000)])
def test_before_17_a_missing_or_loose_fallback_is_unsafe(statement: int, idle: int) -> None:
    """16: either timeout off, or not below the lag, fails closed."""
    verdict = evaluate_timeouts(_facts(None, statement, idle, 160004), LAG)
    assert not verdict.ok
    assert verdict.reason is not None
    assert "no transaction_timeout" in verdict.reason


def test_the_query_reads_the_catalog_and_tolerates_a_missing_parameter() -> None:
    """Each timeout is a scalar subselect of `pg_settings`, so a server without one gives NULL."""
    sql = str(tc._FACTS)
    for name in ("transaction_timeout", "statement_timeout", "idle_in_transaction_session_timeout"):
        assert f"name = '{name}'" in sql


# --- the gate ------------------------------------------------------------------------------


class _Session:
    """A fake session whose query returns `row`, or raises when `row` is None."""

    def __init__(self, row: tuple[object, ...] | None) -> None:
        self.row = row

    def __call__(self) -> _Session:
        return self

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, _stmt: object, _params: object = None) -> Any:  # noqa: ANN401
        if self.row is None:
            raise ConnectionError("down")
        row = self.row
        return types.SimpleNamespace(one=lambda: row)


SAFE = (170002, 15_000, 0, 0)
UNSAFE = (170002, 0, 0, 0)
FALLBACK = (160004, None, 15_000, 15_000)


def _gate(row: tuple[object, ...] | None) -> TimeoutGate:
    return TimeoutGate(_Session(row), LAG)  # type: ignore[arg-type]


def _current(gate: TimeoutGate) -> bool:
    """`gate.current` read through a call, so mypy does not narrow it across a refresh."""
    return gate.current


async def test_the_gate_reads_the_row_and_holds_the_verdict() -> None:
    """A safe row makes the gate current; an unsafe one keeps it not ready with the reason."""
    safe = _gate(SAFE)
    assert not _current(safe)
    assert await safe.refresh()
    assert _current(safe)
    assert safe.reason is None
    unsafe = _gate(UNSAFE)
    assert not await unsafe.refresh()
    assert unsafe.reason is not None
    assert "transaction_timeout is 0 ms" in unsafe.reason


async def test_the_fallback_is_ready_and_logs_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    """PostgreSQL 16 with both timeouts set: ready, with the weaker-bound warning in the log."""
    gate = _gate(FALLBACK)
    with caplog.at_level(logging.WARNING):
        assert await gate.refresh()
    assert "has no transaction_timeout" in caplog.text


async def test_an_unreadable_row_fails_closed() -> None:
    """If the query fails the gate is not ready and says why."""
    gate = _gate(None)
    assert not await gate.refresh()
    assert gate.reason == tc.UNREADABLE_REASON


async def test_mock_mode_is_always_ready_and_touches_no_database() -> None:
    """MODE=mock is ready without a session."""
    gate = TimeoutGate(None, LAG, mock=True)
    assert gate.current
    assert await gate.refresh()


async def test_wait_returns_when_safe_and_raises_after_the_timeout() -> None:
    """`wait` polls; it raises `TimeoutCheckTimeout` once the deadline passes."""
    await _gate(SAFE).wait(timeout=1, poll=0)
    clock = iter([0.0, 0.5, 2.0])
    with pytest.raises(tc.TimeoutCheckTimeout, match="transaction_timeout"):
        await _gate(UNSAFE).wait(timeout=1, poll=0, monotonic=lambda: next(clock))


async def test_production_startup_waits_and_then_says_to_exit() -> None:
    """Production: an unsafe bound yields False (exit non-zero) after the timeout."""
    assert not await _gate(UNSAFE).startup(production=True, timeout=0.02, poll=0.005)
    assert await _gate(SAFE).startup(production=True, timeout=0.02, poll=0.005)


async def test_development_startup_only_warns_and_never_blocks() -> None:
    """Outside production an unsafe bound is reported not ready, and the process carries on."""
    unsafe = _gate(UNSAFE)
    assert await unsafe.startup(production=False, timeout=0.02, poll=0.005)
    assert not unsafe.current
    assert unsafe.reason is not None


async def test_the_api_background_task_exits_only_in_production() -> None:
    """Production exits with 1 after the wait; development keeps polling and never exits."""
    exits: list[int] = []
    await _gate(UNSAFE).background(
        production=True, timeout=0.02, poll=0.005, exit_process=exits.append
    )
    assert exits == [1]
    await _gate(SAFE).background(
        production=True, timeout=0.02, poll=0.005, exit_process=exits.append
    )
    assert exits == [1]
    session = _Session(UNSAFE)
    dev = TimeoutGate(session, LAG)  # type: ignore[arg-type]
    task = asyncio.create_task(
        dev.background(production=False, timeout=0.02, poll=0.005, exit_process=exits.append)
    )
    await asyncio.sleep(0.05)
    assert not task.done()
    assert not _current(dev)
    session.row = SAFE  # the operator fixes the setting
    await asyncio.wait_for(task, timeout=1)
    assert _current(dev)
    assert exits == [1]


# --- readiness and wiring ------------------------------------------------------------------


async def test_the_readiness_check_carries_the_reason() -> None:
    """Unbound or unsafe: unhealthy with a detail; safe: healthy; registering twice is fine."""
    assert not (await TimeoutCheck().check()).healthy
    unsafe = _gate(UNSAFE)
    await unsafe.refresh()
    registry = HealthRegistry()
    assert register_timeout_check(registry, unsafe)
    [result] = await registry.run_all()
    assert result.name == "transaction_timeout"
    assert not result.healthy
    assert result.detail is not None
    assert "AUDIT_EXPORT_LAG_SECONDS" in result.detail
    safe = _gate(SAFE)
    await safe.refresh()
    assert not register_timeout_check(registry, safe)  # already registered: rebinds
    [result] = await registry.run_all()
    assert result.healthy


async def test_the_api_checks_the_timeout_with_the_budget_the_schema_wait_left() -> None:
    """`wait_gates` hands the timeout gate the same remaining budget as the role gate."""
    seen: dict[str, float] = {}
    clock = iter([100.0, 107.0])

    class Schema:
        async def wait_or_exit(self, *, timeout: float, poll: float) -> None:
            return None

    class Gate:
        def __init__(self, name: str) -> None:
            self.name = name

        async def background(self, *, production: bool, timeout: float, poll: float) -> None:
            seen[self.name] = timeout

    core = types.SimpleNamespace(gate=Schema(), roles=Gate("roles"), timeouts=Gate("timeouts"))
    settings = make_settings(schema_wait_seconds=10.0)
    await main_module.wait_gates(core, settings, lambda: next(clock))  # type: ignore[arg-type]
    assert seen == {"roles": pytest.approx(3.0), "timeouts": pytest.approx(3.0)}


async def test_the_worker_exits_non_zero_when_the_timeout_stays_unsafe(
    monkeypatch: pytest.MonkeyPatch, key_signers: None
) -> None:
    """`run_process` exits 1 when the timeout gate says to, after the role gate passed."""
    seen: list[float] = []

    async def role_ok(*_a: object, **_k: object) -> bool:
        return True

    async def unsafe(_self: TimeoutGate, *, timeout: float, **_k: object) -> bool:
        seen.append(timeout)
        return False

    async def no_wait(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(SchemaGate, "wait", no_wait)
    monkeypatch.setattr(RoleGate, "startup", role_ok)
    monkeypatch.setattr(TimeoutGate, "startup", unsafe)
    for kind in ("worker", "reconciler"):
        overrides = ProcessOverrides(
            redis=as_redis(FakeRedis()), checker=FakeHostChecker(), stop=asyncio.Event()
        )
        assert await runtime.run_process(kind, make_settings(), overrides) == 1
    assert len(seen) == 2
    assert all(0 < budget <= 300.0 for budget in seen)
