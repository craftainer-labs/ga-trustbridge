"""The command shims and dispatcher (ADR 0028 "Processes")."""

import runpy
from collections.abc import Coroutine
from typing import Any, cast

import pytest

from trustbridge.worker import __main__ as commands
from trustbridge.worker import migrate, runtime


@pytest.mark.parametrize(
    ("module", "expected"),
    [
        ("trustbridge.reconciler", ["reconciler", "x"]),
        ("trustbridge.migrations", ["migrate", "x"]),
    ],
)
def test_shims_forward_to_the_worker_dispatcher(
    module: str, expected: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shims forward to the worker dispatcher."""
    seen: list[list[str]] = []

    def fake_main(argv: list[str]) -> int:
        seen.append(argv)
        return 7

    monkeypatch.setattr(commands, "main", fake_main)
    monkeypatch.setattr("sys.argv", ["prog", "x"])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module(module, run_name="__main__")
    assert exit_info.value.code == 7
    assert seen == [expected]


def test_dispatcher_routes_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dispatcher routes commands."""
    monkeypatch.setattr(commands, "configure_logging", lambda: None)
    monkeypatch.setattr(migrate, "main", lambda argv: 3 if argv == ["upgrade"] else 9)
    monkeypatch.setattr(runtime, "main", lambda kind: {"worker": 0, "reconciler": 1}[kind])
    assert commands.main(["migrate", "upgrade"]) == 3
    assert commands.main([]) == 0
    assert commands.main(["worker"]) == 0
    assert commands.main(["reconciler"]) == 1
    assert commands.main(["bogus"]) == 2


def test_runtime_main_runs_the_process_and_returns_its_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime main runs the process and returns its code."""
    monkeypatch.setattr(runtime, "configure_logging", lambda: None)
    monkeypatch.setattr(runtime, "run_process", lambda kind: kind)

    def run(coro: Coroutine[Any, Any, int]) -> int:
        return 5 if cast("Any", coro) == "worker" else 6

    assert runtime.main("worker", runner=run) == 5
