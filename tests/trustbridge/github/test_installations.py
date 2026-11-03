"""`AppInstallationLookup`: the installation's current state read with the App JWT."""

import pytest

from tests.trustbridge.github.conftest import Env
from trustbridge.github.availability import HostNotReadyError
from trustbridge.github.client import GitHubApi, GitHubApiError, GitHubRequester
from trustbridge.github.hosts import GitHubHostConfig
from trustbridge.github.installations import AppInstallationLookup, InstallationInfo

PATH = "/app/installations/4242"


def _lookup(env: Env) -> AppInstallationLookup:
    def factory(host: GitHubHostConfig) -> GitHubRequester:
        return GitHubApi(host, async_transport=env.gh.transport())

    return AppInstallationLookup(env.monitor, {"main": env.signer}, factory)


async def test_an_active_installation_is_read_with_the_app_jwt(env: Env) -> None:
    """`suspended_at: null` is active; the call carries the App JWT."""
    env.gh.route(
        "GET", PATH, body={"id": 4242, "account": {"login": "acme", "id": 5}, "suspended_at": None}
    )
    info = await _lookup(env).fetch("github.com", 4242)
    assert info == InstallationInfo("acme", 5, False)
    request = next(r for r in env.gh.requests if r.url.path == PATH)
    assert request.headers["Authorization"].startswith("Bearer ")


async def test_a_suspended_installation_is_reported_suspended(env: Env) -> None:
    """A `suspended_at` timestamp means suspended; a missing account yields empty identity."""
    env.gh.route("GET", PATH, body={"id": 4242, "suspended_at": "2026-11-01T00:00:00Z"})
    assert await _lookup(env).fetch("github.com", 4242) == InstallationInfo("", 0, True)


async def test_a_missing_installation_is_none(env: Env) -> None:
    """404 means the installation is gone."""
    env.gh.route("GET", PATH, status=404, body={"message": "Not Found"})
    assert await _lookup(env).fetch("github.com", 4242) is None


async def test_other_errors_propagate_so_the_delivery_is_retried(env: Env) -> None:
    """A 5xx is not "gone"."""
    env.gh.route("GET", PATH, status=502, body={})
    with pytest.raises(GitHubApiError) as err:
        await _lookup(env).fetch("github.com", 4242)
    assert err.value.retryable


async def test_a_host_that_is_not_ready_makes_no_call(env: Env) -> None:
    """No GitHub call is made for an unavailable host."""
    env.checker.issuer = "https://evil.example"
    await env.monitor.check_all()
    before = len(env.gh.requests)
    with pytest.raises(HostNotReadyError):
        await _lookup(env).fetch("github.com", 4242)
    assert len(env.gh.requests) == before
