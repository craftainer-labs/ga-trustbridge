"""The writer records the environments it creates, and only those (ADR 0034 decision B6)."""

import json
from uuid import UUID

import pytest
from sqlalchemy.exc import OperationalError

from tests.trustbridge.github.conftest import Env
from tests.trustbridge.github.test_tokens_writer import script_repo
from trustbridge.adapters.interface import GitHubOutput
from trustbridge.adapters.store import OutputError
from trustbridge.github.writer import GitHubOutputWriter

PATH = "/repos/acme/web/environments/prod"
ENVIRONMENT = GitHubOutput("environment", "prod", "", 10, None)


class Recorder:
    """An `EnvironmentTracker` that keeps what it was told, or fails."""

    def __init__(self, error: Exception | None = None) -> None:
        """Fail every call with `error` when given."""
        self.calls: list[tuple[UUID, str, int, str]] = []
        self.error = error

    async def record_created(
        self, tenant_id: UUID, host: str, repository_id: int, name: str
    ) -> None:
        """Record the call."""
        if self.error is not None:
            raise self.error
        self.calls.append((tenant_id, host, repository_id, name))


async def test_an_environment_the_writer_creates_is_recorded_after_the_put(env: Env) -> None:
    """GET answers 404, the PUT creates it, then one row is recorded (host, repository, name)."""
    script_repo(env)
    env.gh.route("GET", PATH, 404, {})
    env.gh.route("PUT", PATH, 200, {})
    tracker = Recorder()
    await GitHubOutputWriter(env.gateway, tracker).apply(env.tenant, [ENVIRONMENT])
    assert tracker.calls == [(env.tenant.id, "github.com", 10, "prod")]
    methods = [r.method for r in env.gh.requests if r.url.path == PATH]
    assert methods == ["GET", "PUT"]  # the check comes first; a crash before the row keeps it
    scopes = [
        json.loads(r.content).get("permissions")
        for r in env.gh.requests
        if r.url.path == "/app/installations/4242/access_tokens"
    ]
    assert {"administration": "read"} in scopes  # the GET only reads
    assert {"administration": "write"} in scopes  # the PUT writes


async def test_an_environment_that_already_exists_records_nothing(env: Env) -> None:
    """GET answers 200: the writer did not create it, so nothing is tracked."""
    script_repo(env)
    env.gh.route("GET", PATH, 200, {"protection_rules": [{"type": "required_reviewers"}]})
    env.gh.route("PUT", PATH, 200, {})
    tracker = Recorder()
    await GitHubOutputWriter(env.gateway, tracker).apply(env.tenant, [ENVIRONMENT])
    assert tracker.calls == []


async def test_a_failing_existence_check_is_an_output_error_and_creates_nothing(env: Env) -> None:
    """Only a 404 means absent; any other answer stops before the PUT."""
    script_repo(env)
    env.gh.route("GET", PATH, 500, {"message": "x"})
    tracker = Recorder()
    with pytest.raises(OutputError) as failed:
        await GitHubOutputWriter(env.gateway, tracker).apply(env.tenant, [ENVIRONMENT])
    assert failed.value.retryable is True
    assert tracker.calls == []
    assert not any(r.method == "PUT" and r.url.path == PATH for r in env.gh.requests)


async def test_a_failed_put_records_nothing(env: Env) -> None:
    """The row is written only after the creation succeeded."""
    script_repo(env)
    env.gh.route("GET", PATH, 404, {})
    env.gh.route("PUT", PATH, 422, {"message": "x"})
    tracker = Recorder()
    with pytest.raises(OutputError):
        await GitHubOutputWriter(env.gateway, tracker).apply(env.tenant, [ENVIRONMENT])
    assert tracker.calls == []


async def test_a_failing_record_is_a_retryable_output_error_without_the_cause_text(
    env: Env,
) -> None:
    """The database failure text (which may name tenant data) is not in the error."""
    script_repo(env)
    env.gh.route("GET", PATH, 404, {})
    env.gh.route("PUT", PATH, 200, {})
    tracker = Recorder(OperationalError("INSERT secret-text", {}, Exception("secret-text")))
    with pytest.raises(OutputError) as failed:
        await GitHubOutputWriter(env.gateway, tracker).apply(env.tenant, [ENVIRONMENT])
    assert failed.value.retryable is True
    assert "secret-text" not in str(failed.value)


async def test_without_a_tracker_the_writer_makes_no_existence_check(env: Env) -> None:
    """The previous behaviour: one PUT and no GET (an N-1 writer records nothing)."""
    script_repo(env)
    env.gh.route("PUT", PATH, 200, {})
    await GitHubOutputWriter(env.gateway).apply(env.tenant, [ENVIRONMENT])
    assert [r.method for r in env.gh.requests if r.url.path == PATH] == ["PUT"]
