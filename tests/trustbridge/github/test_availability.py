"""Per-host availability: unregistered / unavailable / ready, rechecks, registration overlay."""

import asyncio

import pytest

from tests.trustbridge.github.conftest import FakeChecker, KeySigner, make_host_config
from trustbridge.github.availability import (
    HostMonitor,
    HostNotReadyError,
    HostState,
    MonitorDeps,
    effective_config,
)
from trustbridge.github.client import GitHubApiError
from trustbridge.github.hosts import GitHubHostConfig
from trustbridge.github.models import Registration

WEBHOOK_REF = "ref/webhook"


class Registrations:
    """A mutable `RegistrationStore`."""

    def __init__(self) -> None:
        """Start with none."""
        self.items: dict[str, Registration] = {}
        self.fail = False

    async def load_all(self) -> dict[str, Registration]:
        """Return the stored registrations (or fail)."""
        if self.fail:
            raise RuntimeError("db down")
        return dict(self.items)


async def test_right_key_makes_the_host_ready_and_probes_with_a_jwt() -> None:
    """Invariant 1 (pass side): the probe verifies and `GET /app` returns the App id."""
    checker = FakeChecker(app_id=77)
    mon = HostMonitor([make_host_config()], {"main": KeySigner()}, MonitorDeps(checker=checker))
    await mon.check_all()
    assert mon.status("github.com").state is HostState.READY
    assert checker.last_jwt is not None
    assert checker.last_jwt.count(".") == 2
    assert mon.require_ready("github.com").app_id == 77


async def test_key_of_another_app_fails_the_self_test() -> None:
    """Invariant 1 (fail side): GitHub reports a different id, so the host is unavailable."""
    mon = HostMonitor(
        [make_host_config()], {"main": KeySigner()}, MonitorDeps(checker=FakeChecker(app_id=999))
    )
    await mon.check_all()
    status = mon.status("github.com")
    assert status.state is HostState.UNAVAILABLE
    assert status.reason is not None
    assert "app_id" in status.reason


async def test_signature_that_does_not_verify_against_its_public_key_fails() -> None:
    """A signer whose advertised public key is not the signing key is unavailable."""
    other = KeySigner().key
    mon = HostMonitor(
        [make_host_config()],
        {"main": KeySigner(advertised=other)},
        MonitorDeps(checker=FakeChecker()),
    )
    await mon.check_all()
    assert mon.status("github.com").state is HostState.UNAVAILABLE


async def test_issuer_mismatch_makes_the_host_unavailable_but_nothing_raises() -> None:
    """Invariant 4: a differing discovery issuer is a reason, not a crash."""
    mon = HostMonitor(
        [make_host_config()],
        {"main": KeySigner()},
        MonitorDeps(checker=FakeChecker(issuer="https://evil.example")),
    )
    await mon.check_all()
    status = mon.status("github.com")
    assert status.state is HostState.UNAVAILABLE
    assert status.reason == "OIDC discovery issuer differs from the configured oidc_issuer"


async def test_signer_outage_for_one_host_leaves_the_others_alone() -> None:
    """Invariant 7: a failing signer is contained to its host; check_all returns normally."""
    bad, good = KeySigner(), KeySigner()
    bad.fail = True
    h1 = make_host_config("a").model_copy(update={"signer": "bad"})
    mon = HostMonitor(
        [h1, make_host_config("b")], {"bad": bad, "main": good}, MonitorDeps(checker=FakeChecker())
    )
    await mon.check_all()
    assert mon.status("a").state is HostState.UNAVAILABLE
    assert mon.status("b").state is HostState.READY
    with pytest.raises(HostNotReadyError):
        mon.require_ready("a")


async def test_github_rejection_is_unavailable() -> None:
    """A 401 from `GET /app` is a reason, not an exception."""

    class Rejecting(FakeChecker):
        async def get_app_id(self, host: GitHubHostConfig, jwt: str) -> int:
            raise GitHubApiError(401, "GET /app")

    mon = HostMonitor([make_host_config()], {"main": KeySigner()}, MonitorDeps(checker=Rejecting()))
    await mon.check_all()
    assert mon.status("github.com").reason == "GitHub rejected the App JWT (HTTP 401)"


async def test_unregistered_host_becomes_ready_after_registration_without_restart() -> None:
    """Invariant 9: the stored registration is overlaid and picked up by the next recheck."""
    regs = Registrations()
    checker = FakeChecker(app_id=55)
    mon = HostMonitor(
        [make_host_config(app_id=None)],
        {"main": KeySigner()},
        MonitorDeps(checker=checker, registrations=regs),
    )
    await mon.check_all()
    assert mon.status("github.com").state is HostState.UNREGISTERED
    assert checker.app_calls == 0
    regs.items["github.com"] = Registration(55, "ref/webhook")
    await mon.recheck()
    assert mon.status("github.com").state is HostState.READY
    assert mon.config("github.com").webhook_secret_ref == WEBHOOK_REF


async def test_unavailable_host_recovers_and_on_ready_fires_once() -> None:
    """Invariants 8 and 9: recheck turns it ready; the hook runs on the transition only."""
    ready: list[str] = []

    async def on_ready(cfg: GitHubHostConfig) -> None:
        ready.append(cfg.name)

    checker = FakeChecker(app_id=1)
    mon = HostMonitor(
        [make_host_config()], {"main": KeySigner()}, MonitorDeps(checker=checker, on_ready=on_ready)
    )
    await mon.check_all()
    assert mon.status("github.com").state is HostState.UNAVAILABLE
    checker.app_id = 77
    await mon.recheck()
    await mon.recheck()
    assert mon.status("github.com").state is HostState.READY
    assert ready == ["github.com"]
    assert checker.app_calls == 2  # the ready host is not probed again


async def test_failing_on_ready_hook_does_not_unready_the_host() -> None:
    """A hook error is logged and ignored."""

    async def boom(cfg: GitHubHostConfig) -> None:
        raise RuntimeError("redis down")

    mon = HostMonitor(
        [make_host_config()],
        {"main": KeySigner()},
        MonitorDeps(checker=FakeChecker(), on_ready=boom),
    )
    await mon.check_all()
    assert mon.status("github.com").state is HostState.READY


async def test_registration_store_outage_keeps_the_previous_view() -> None:
    """The database being down does not flip a ready host."""
    regs = Registrations()
    regs.items["github.com"] = Registration(77, "ref/webhook")
    mon = HostMonitor(
        [make_host_config(app_id=None)],
        {"main": KeySigner()},
        MonitorDeps(checker=FakeChecker(), registrations=regs),
    )
    await mon.check_all()
    regs.fail = True
    await mon.check_all()
    assert mon.status("github.com").state is HostState.READY


def test_configured_app_wins_over_a_stored_registration() -> None:
    """An entry that already has an App is never overridden."""
    cfg = make_host_config(app_id=1)
    assert effective_config(cfg, {"github.com": Registration(2, "x")}).app_id == 1


async def test_recheck_loop_runs_until_stopped() -> None:
    """The background task rechecks on its interval and stops cleanly."""
    checker = FakeChecker(app_id=1)
    mon = HostMonitor([make_host_config()], {"main": KeySigner()}, MonitorDeps(checker=checker))
    await mon.check_all()
    checker.app_id = 77
    mon.start(0.01)
    for _ in range(100):
        await asyncio.sleep(0.01)
        if mon.status("github.com").state is HostState.READY:
            break
    await mon.stop()
    assert mon.status("github.com").state is HostState.READY
    assert mon.statuses()["github.com"].state is HostState.READY
