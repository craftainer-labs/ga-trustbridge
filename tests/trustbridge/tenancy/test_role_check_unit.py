"""The runtime role safety check's decision logic and wiring, without a database.

ADR 0031 "Role safety checks" (second bullet) and invariant 16. The Postgres-backed half is in
`test_role_check_integration.py`.
"""

import asyncio
import types
from typing import Any

import pytest
from craftainer_observability.health.registry import HealthRegistry

from tests.trustbridge.fakes import FakeHostChecker, FakeRedis
from tests.trustbridge.helpers import as_redis, make_settings
from trustbridge import main as main_module
from trustbridge.tenancy import (
    RoleCheck,
    RoleFacts,
    RoleGate,
    evaluate,
    register_role_check,
)
from trustbridge.tenancy import role_check as role_module
from trustbridge.worker import runtime
from trustbridge.worker.runtime import ProcessOverrides


def _facts(*, superuser: bool = False, bypassrls: bool = False, forced: bool = True) -> RoleFacts:
    return RoleFacts("trustbridge_app", superuser, bypassrls, forced)


class _Session:
    """A fake session whose role query returns `row`, or raises when `row` is None."""

    def __init__(self, row: tuple[object, ...] | None) -> None:
        self.row = row
        self.calls = 0

    def __call__(self) -> _Session:
        return self

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, _stmt: object, _params: object = None) -> Any:  # noqa: ANN401
        self.calls += 1
        if self.row is None:
            raise ConnectionError("down")
        row = self.row
        return types.SimpleNamespace(one=lambda: row)


def _current(gate: RoleGate) -> bool:
    """`gate.current` read through a call, so mypy does not narrow it across a refresh."""
    return gate.current


def _gate(row: tuple[object, ...] | None) -> RoleGate:
    return RoleGate(_Session(row), mock=False)  # type: ignore[arg-type]


# --- the decision (inv. 16) ---------------------------------------------------------------


def test_a_normal_role_is_safe() -> None:
    """A role that is neither a superuser nor BYPASSRLS is safe, forced tables or not."""
    assert evaluate(_facts()).ok
    assert evaluate(_facts()).reason is None


def test_a_superuser_is_unsafe_once_a_table_is_forced() -> None:
    """A superuser reports not ready with the reason."""
    verdict = evaluate(_facts(superuser=True))
    assert not verdict.ok
    assert verdict.reason is not None
    assert "trustbridge_app" in verdict.reason
    assert "a superuser" in verdict.reason


def test_bypassrls_is_unsafe_once_a_table_is_forced() -> None:
    """A BYPASSRLS role reports not ready with the reason."""
    verdict = evaluate(_facts(bypassrls=True))
    assert not verdict.ok
    assert verdict.reason is not None
    assert "BYPASSRLS" in verdict.reason


def test_both_attributes_are_named_in_the_reason() -> None:
    """A superuser that also has BYPASSRLS gets both in the reason."""
    verdict = evaluate(_facts(superuser=True, bypassrls=True))
    assert verdict.reason is not None
    assert "a superuser and BYPASSRLS" in verdict.reason


def test_the_check_is_skipped_while_no_rls_table_is_forced() -> None:
    """With nothing forced yet (release N), even a superuser is accepted."""
    assert evaluate(_facts(superuser=True, bypassrls=True, forced=False)).ok


def test_audit_events_counts_as_an_rls_table() -> None:
    """`audit_events` (ADR 0031 "seventh RLS table") is among the tables that arm the check."""
    assert "audit_events" in role_module.RLS_TABLES
    assert len(role_module.RLS_TABLES) == 7


# --- the gate ------------------------------------------------------------------------------


async def test_the_gate_reads_the_row_and_holds_the_verdict() -> None:
    """A safe row makes the gate current; an unsafe one keeps it not ready with the reason."""
    safe = _gate(("trustbridge_app", False, False, True))
    assert not _current(safe)
    assert await safe.refresh()
    assert _current(safe)
    assert safe.reason is None
    unsafe = _gate(("postgres", True, False, True))
    assert not await unsafe.refresh()
    assert not unsafe.current
    assert unsafe.reason is not None
    assert "postgres" in unsafe.reason


async def test_an_unreadable_role_fails_closed() -> None:
    """If the query fails the gate is not ready and says why."""
    gate = _gate(None)
    assert not await gate.refresh()
    assert gate.reason == role_module.UNREADABLE_REASON


async def test_mock_mode_is_always_ready_and_touches_no_database() -> None:
    """MODE=mock is ready without a session."""
    gate = RoleGate(None, mock=True)
    assert gate.current
    assert await gate.refresh()


async def test_wait_returns_when_the_role_is_safe_and_raises_after_the_timeout() -> None:
    """`wait` polls; it raises `RoleCheckTimeout` once the deadline passes."""
    await _gate(("trustbridge_app", False, False, True)).wait(timeout=1, poll=0)
    clock = iter([0.0, 0.5, 2.0])
    with pytest.raises(role_module.RoleCheckTimeout, match="superuser"):
        await _gate(("postgres", True, False, True)).wait(
            timeout=1, poll=0, monotonic=lambda: next(clock)
        )


async def test_production_startup_waits_and_then_says_to_exit() -> None:
    """Production: an unsafe role yields False (exit non-zero) after the timeout."""
    unsafe = _gate(("postgres", True, False, True))
    assert not await unsafe.startup(production=True, timeout=0.02, poll=0.005)
    safe = _gate(("trustbridge_app", False, False, True))
    assert await safe.startup(production=True, timeout=0.02, poll=0.005)


async def test_development_startup_only_reports_and_never_blocks() -> None:
    """Outside production an unsafe role is reported not ready, and the process carries on."""
    unsafe = _gate(("postgres", True, False, True))
    assert await unsafe.startup(production=False, timeout=0.02, poll=0.005)
    assert not unsafe.current
    assert unsafe.reason is not None


async def test_the_api_background_task_exits_only_in_production() -> None:
    """Production exits with 1 after the wait; development keeps polling and never exits."""
    exits: list[int] = []
    unsafe = _gate(("postgres", True, False, True))
    await unsafe.background(production=True, timeout=0.02, poll=0.005, exit_process=exits.append)
    assert exits == [1]
    safe = _gate(("trustbridge_app", False, False, True))
    await safe.background(production=True, timeout=0.02, poll=0.005, exit_process=exits.append)
    assert exits == [1]
    dev_session = _Session(("postgres", True, False, True))
    dev = RoleGate(dev_session, mock=False)  # type: ignore[arg-type]
    task = asyncio.create_task(
        dev.background(production=False, timeout=0.02, poll=0.005, exit_process=exits.append)
    )
    await asyncio.sleep(0.05)
    assert not task.done()  # still polling, not ready, not exited
    assert not _current(dev)
    dev_session.row = ("trustbridge_app", False, False, True)  # the operator fixes the role
    await asyncio.wait_for(task, timeout=1)
    assert _current(dev)
    assert exits == [1]


# --- readiness ----------------------------------------------------------------------------


async def test_the_readiness_check_carries_the_reason() -> None:
    """Unbound or unsafe: unhealthy with a detail; safe: healthy; registering twice is fine."""
    assert not (await RoleCheck().check()).healthy
    unsafe = _gate(("postgres", True, False, True))
    await unsafe.refresh()
    registry = HealthRegistry()
    assert register_role_check(registry, unsafe)
    [result] = await registry.run_all()
    assert result.name == "role_safety"
    assert not result.healthy
    assert result.detail is not None
    assert "postgres" in result.detail
    safe = _gate(("trustbridge_app", False, False, True))
    await safe.refresh()
    assert not register_role_check(registry, safe)  # already registered: rebinds
    [result] = await registry.run_all()
    assert result.healthy


# --- hook-in ------------------------------------------------------------------------------


async def test_the_api_waits_for_the_schema_then_checks_the_role() -> None:
    """`wait_gates` runs the schema wait, then the role gate, with the settings' timings."""
    calls: list[str] = []

    class Schema:
        async def wait_or_exit(self, *, timeout: float, poll: float) -> None:
            calls.append(f"schema:{timeout}:{poll}")

    class Roles:
        async def background(self, *, production: bool, timeout: float, poll: float) -> None:
            calls.append(f"roles:{production}:{timeout}:{poll}")

    core = types.SimpleNamespace(gate=Schema(), roles=Roles())
    settings = make_settings(schema_wait_seconds=3.0, schema_poll_seconds=0.5)
    await main_module.wait_gates(core, settings)  # type: ignore[arg-type]
    assert calls == ["schema:3.0:0.5", "roles:False:3.0:0.5"]


async def test_the_worker_and_reconciler_return_non_zero_when_the_role_stays_unsafe(
    monkeypatch: pytest.MonkeyPatch, key_signers: None
) -> None:
    """`run_process` (worker and reconciler) exits 1 when the role gate says to."""

    async def unsafe(*_a: object, **_k: object) -> bool:
        return False

    monkeypatch.setattr(RoleGate, "startup", unsafe)
    for kind in ("worker", "reconciler"):
        overrides = ProcessOverrides(
            redis=as_redis(FakeRedis()), checker=FakeHostChecker(), stop=asyncio.Event()
        )
        assert await runtime.run_process(kind, make_settings(), overrides) == 1
