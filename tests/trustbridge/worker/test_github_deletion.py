"""`SqlGithubPort`'s GitHub-facing calls at unit level (NG6h-6); rows are in the integration."""

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest

from tests.trustbridge.adapters.conftest import FixedTenants, make_tenant
from trustbridge.adapters.memory import RecordingWriter
from trustbridge.deletion.ports import TrackedEnvironment
from trustbridge.github.client import ApiResponse, GitHubApiError
from trustbridge.github.gateway import Call
from trustbridge.github.installations import InstallationInfo
from trustbridge.worker.github_deletion import SqlGithubPort

ENV = TrackedEnvironment(10, "prod env", datetime(2026, 11, 14, tzinfo=UTC))


class FakeGateway:
    """Answers `full_name` and `call`, recording the calls; errors are queued by method."""

    def __init__(self) -> None:
        """Start with a healthy GitHub."""
        self.calls: list[Call] = []
        self.errors: dict[str, GitHubApiError] = {}
        self.full_name_error: GitHubApiError | None = None

    async def full_name(self, tenant: object, repository_id: int) -> str:
        """The repository's name, or its error."""
        assert repository_id == 10
        if self.full_name_error is not None:
            raise self.full_name_error
        return "acme/web"

    async def call(self, tenant: object, request: Call) -> ApiResponse:
        """Record the call; answer an environment, or raise."""
        self.calls.append(request)
        if request.method in self.errors:
            raise self.errors[request.method]
        return ApiResponse(200, {"created_at": "2026-11-14T12:00:00Z"})


class FakeInstallations:
    """Says whether the installation exists."""

    def __init__(self, *, exists: bool) -> None:
        """Hold the answer."""
        self.exists = exists
        self.asked: list[tuple[str, int]] = []

    async def fetch(self, host: str, installation_id: int) -> InstallationInfo | None:
        """The installation, or None."""
        self.asked.append((host, installation_id))
        return InstallationInfo("acme", 1, suspended=True) if self.exists else None


class FakeEnvironments:
    """Tracked rows with times."""

    async def tracked(self, tenant_id: UUID) -> list[tuple[int, str]]:
        """Names only."""
        return [(ENV.repository_id, ENV.name)]

    async def tracked_since(self, tenant_id: UUID) -> list[tuple[int, str, datetime]]:
        """Names and times."""
        return [(ENV.repository_id, ENV.name, ENV.created_at)]

    async def forget(self, tenant_id: UUID, repository_id: int, name: str) -> None:
        """Nothing to forget here."""


def _port(gateway: FakeGateway, *, exists: bool = True) -> tuple[SqlGithubPort, UUID]:
    tenant = make_tenant()
    port = SqlGithubPort(
        sessions=cast("Any", None),
        tenants=FixedTenants(tenant),
        writer=RecordingWriter(),
        gateway=cast("Any", gateway),
        environments=FakeEnvironments(),
        installations=FakeInstallations(exists=exists),
    )
    return port, tenant.id


async def test_a_suspended_installation_still_exists_and_a_missing_one_does_not() -> None:
    """Existence is the lookup's answer; suspension is not absence."""
    port, _ = _port(FakeGateway())
    assert await port.installation_exists("github.com", 42)
    gone, _ = _port(FakeGateway(), exists=False)
    assert not await gone.installation_exists("github.com", 42)


async def test_tracked_environments_carry_their_row_time() -> None:
    """The port turns the store's tuples into `TrackedEnvironment`s."""
    port, tenant = _port(FakeGateway())
    assert await port.environments(tenant) == [ENV]


async def test_the_environments_created_at_is_read_with_a_scoped_get() -> None:
    """`GET /repos/<full>/environments/<name>`, `administration: read`, the one repository."""
    gateway = FakeGateway()
    port, tenant = _port(gateway)
    created = await port.environment_created_at(tenant, ENV)
    assert created == datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
    (call,) = gateway.calls
    assert (call.method, call.path) == ("GET", "/repos/acme/web/environments/prod%20env")
    assert dict(call.permissions) == {"administration": "read"}
    assert call.repository_ids == [10]


async def test_a_missing_environment_or_repository_has_no_created_at() -> None:
    """404 on the environment, or on the repository lookup, means it is gone."""
    gateway = FakeGateway()
    port, tenant = _port(gateway)
    gateway.errors["GET"] = GitHubApiError(404, "Not Found")
    assert await port.environment_created_at(tenant, ENV) is None
    gateway.errors.clear()
    gateway.full_name_error = GitHubApiError(404, "Not Found")
    assert await port.environment_created_at(tenant, ENV) is None


async def test_other_github_errors_are_raised() -> None:
    """A 5xx is transient for the engine, never read as "gone"."""
    gateway = FakeGateway()
    port, tenant = _port(gateway)
    gateway.errors["GET"] = GitHubApiError(503, "down")
    with pytest.raises(GitHubApiError):
        await port.environment_created_at(tenant, ENV)
    gateway.errors["DELETE"] = GitHubApiError(403, "no")
    with pytest.raises(GitHubApiError):
        await port.delete_environment(tenant, ENV)


async def test_an_environment_is_deleted_with_a_scoped_delete_and_404_is_fine() -> None:
    """`DELETE` with `administration: write`; an absent environment or repository is done."""
    gateway = FakeGateway()
    port, tenant = _port(gateway)
    await port.delete_environment(tenant, ENV)
    (call,) = gateway.calls
    assert (call.method, call.path) == ("DELETE", "/repos/acme/web/environments/prod%20env")
    assert dict(call.permissions) == {"administration": "write"}
    gateway.errors["DELETE"] = GitHubApiError(404, "Not Found")
    await port.delete_environment(tenant, ENV)
    gateway.errors.clear()
    gateway.full_name_error = GitHubApiError(404, "Not Found")
    await port.delete_environment(tenant, ENV)
