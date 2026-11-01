"""The command shims and dispatcher (ADR 0028 "Processes")."""

import runpy

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
    seen: list[list[str]] = []
    monkeypatch.setattr(commands, "main", lambda argv: seen.append(argv) or 7)
    monkeypatch.setattr("sys.argv", ["prog", "x"])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module(module, run_name="__main__")
    assert exit_info.value.code == 7
    assert seen == [expected]


def test_dispatcher_routes_commands(monkeypatch: pytest.MonkeyPatch) -> None:
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
    monkeypatch.setattr(runtime, "configure_logging", lambda: None)
    monkeypatch.setattr(runtime, "run_process", lambda kind: kind)
    assert runtime.main("worker", runner=lambda coro: 5 if coro == "worker" else 6) == 5
