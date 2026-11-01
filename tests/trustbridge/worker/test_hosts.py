"""Per-host availability and recheck (ADR 0030 invariants 4, 7 and 9)."""

import asyncio

from tests.trustbridge.fakes import FakeHostChecker
from tests.trustbridge.helpers import host
from trustbridge.settings import GitHubHostConfig
from trustbridge.worker.hosts import (
    HostAvailability,
    HostState,
    UninstalledHostChecker,
)


def _hosts(checker: FakeHostChecker | None = None, **kw: object) -> tuple[HostAvailability, list[GitHubHostConfig]]:
    configs = [host("a"), host("b"), host("c", app_id=None, webhook_secret_ref=None)]
    availability = HostAvailability(
        lambda: configs, checker or FakeHostChecker(), check_timeout=float(kw.get("timeout", 1))  # type: ignore[arg-type]
    )
    return availability, configs


async def test_a_failed_check_makes_only_that_host_unavailable_and_never_raises() -> None:
    checker = FakeHostChecker(failing={"a"})  # 0030 invariants 4 and 7
    availability, _ = _hosts(checker)
    await availability.check_all()
    a, b, c = (availability.status(n) for n in "abc")
    assert (a.state, a.reason) == (HostState.UNAVAILABLE, "signer self-test failed")  # type: ignore[union-attr]
    assert b.state is HostState.READY  # type: ignore[union-attr]
    assert c.state is HostState.UNREGISTERED  # type: ignore[union-attr]
    assert checker.calls.count("c") == 0  # an unregistered host skips the network checks


async def test_a_check_that_hangs_or_raises_marks_the_host_unavailable() -> None:

    class Hangs:
        async def check(self, host: GitHubHostConfig) -> None:
            if host.name == "a":
                await asyncio.sleep(5)
            if host.name == "b":
                raise RuntimeError("secret detail")

    availability = HostAvailability(lambda: [host("a"), host("b")], Hangs(), check_timeout=0.05)
    await availability.check_all()
    assert availability.status("a").reason == "check timed out"  # type: ignore[union-attr]
    assert availability.status("b").reason == "check raised RuntimeError"  # type: ignore[union-attr]
    assert "secret detail" not in str(availability.status("b"))


async def test_without_a_real_checker_every_registered_host_fails_closed() -> None:
    availability = HostAvailability(lambda: [host("a")], UninstalledHostChecker(), check_timeout=1)
    await availability.check_all()
    assert availability.status("a").state is HostState.UNAVAILABLE  # type: ignore[union-attr]
    assert not availability.is_ready("a")
    assert availability.status("missing") is None


async def test_the_recheck_moves_an_unavailable_host_to_ready_without_a_restart() -> None:  # 9
    checker = FakeHostChecker(failing={"a"})
    availability, _ = _hosts(checker)
    ready: list[str] = []

    async def listener(name: str) -> None:
        ready.append(name)

    availability.add_ready_listener(listener)
    await availability.check_all()
    assert ready == ["b"]
    task = asyncio.create_task(availability.run_recheck(0.01))
    await asyncio.sleep(0.05)
    assert availability.is_ready("a") is False
    checker.failing.clear()
    await asyncio.sleep(0.1)
    task.cancel()
    assert availability.is_ready("a")
    assert ready == ["b", "a"]
    assert checker.calls.count("b") == 1  # a ready host is not rechecked


async def test_an_unregistered_host_is_picked_up_when_the_host_list_changes() -> None:  # 9
    configs = [host("c", app_id=None, webhook_secret_ref=None)]
    checker = FakeHostChecker(failing={"c"})
    availability = HostAvailability(lambda: configs, checker, check_timeout=1)
    await availability.check_all()
    assert availability.status("c").state is HostState.UNREGISTERED  # type: ignore[union-attr]
    configs[:] = [host("c")]  # the registration flow completed
    await availability.check_all(only_not_ready=True)
    assert availability.status("c").state is HostState.UNAVAILABLE  # type: ignore[union-attr]
    checker.failing.clear()
    await availability.check_all(only_not_ready=True)
    assert availability.is_ready("c")


async def test_a_host_removed_from_the_configuration_is_dropped() -> None:
    availability, configs = _hosts()
    await availability.check_all()
    configs.pop(0)
    await availability.check_all(only_not_ready=True)
    assert availability.config("a") is None
    assert sorted(availability.names()) == ["b", "c"]


async def test_a_failing_listener_does_not_break_the_check() -> None:
    availability, _ = _hosts()

    async def bad(name: str) -> None:
        raise RuntimeError(name)

    availability.add_ready_listener(bad)
    await availability.check_all()
    assert availability.is_ready("a")
