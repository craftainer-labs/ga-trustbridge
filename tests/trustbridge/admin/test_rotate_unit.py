"""Credential rotation through the admin API over the in-memory credential core (NG6e-5)."""

import asyncio
import logging
from dataclasses import replace
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from fastapi.routing import APIRoute

from tests.trustbridge.adapters.conftest import PULL, PUSH_PROD, World, build_world
from tests.trustbridge.admin.helpers import Env, build, problem, token
from trustbridge.adapters.core import (
    CredentialCore,
    OperationFailedError,
    RetryableOperationError,
    RotationNotApplicableError,
    TenantDisabledError,
)
from trustbridge.adapters.fake import fake_capabilities
from trustbridge.adapters.interface import AdapterConfigurationError, AdapterError, scope_key
from trustbridge.adapters.store import CredentialStore, LeaseLostError, OutputError
from trustbridge.admin import guards, routes
from trustbridge.admin.credentials import CredentialRuntime, build_runtime
from trustbridge.audit import Action, AuditEvent, AuditWriter, Outcome
from trustbridge.github.availability import HostNotReadyError, HostState, HostStatus
from trustbridge.github.client import GitHubApiError
from trustbridge.rbac import Role, TenantRef
from trustbridge.worker.lease import LeaseBusy, LeaseLost
from trustbridge.worker.ports import UnknownTenantError
from trustbridge.worker.secrets import SecretUnavailable

OPERATOR = "trustbridge:operator"
ACME_OPERATOR = "trustbridge:github.com:acme:operator"
OTHER_OPERATOR = "trustbridge:github.com:other:operator"
VIEWER = "trustbridge:viewer"
CANARY = "canary-secret-3b8d41"


class Writer(AuditWriter):
    """An `AuditWriter` that records instead of writing."""

    def __init__(self) -> None:
        """Start empty."""
        self.written: list[AuditEvent] = []
        self.error: Exception | None = None

    async def record(self, event: AuditEvent) -> UUID:
        """Record the event, or fail."""
        if self.error is not None:
            raise self.error
        self.written.append(event)
        return event.id


def _path(tenant_id: UUID, adapter: str, key: str) -> str:
    return f"/admin/v1/tenants/{tenant_id}/credentials/{adapter}/{key}/rotate"


def _client(env: Env, *roles: str) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=env.app, raise_app_exceptions=False)
    return httpx.AsyncClient(
        transport=transport, base_url="http://admin.test", headers=token("u1", *roles)
    )


def _env(runtime: CredentialRuntime | None, writer: Writer | None, tenant: UUID) -> Env:
    env = build(tenants=[TenantRef(tenant, "github.com", "acme")])
    env.app.state.admin = replace(env.runtime, writer=writer, credentials=runtime)
    return env


async def _rotating() -> tuple[World, CredentialRuntime, Writer, Env, str]:
    w = build_world(fake_capabilities(oidc=False))
    await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    runtime = CredentialRuntime(w.core, w.store, w.deps.tenants, frozenset({"fake"}))
    writer = Writer()
    key = scope_key(PUSH_PROD.repository_id, PUSH_PROD.environment, PUSH_PROD.permission)
    return w, runtime, writer, _env(runtime, writer, w.tenant.id), key


def test_the_route_is_guarded_for_operators_on_the_tenant_and_emits_credential_rotate() -> None:
    """GUARDED has the tenant-scoped operator guard; the walk accepts the route."""
    api = [r for r in routes.router.routes if isinstance(r, APIRoute)]
    (route,) = [r for r in api if r.endpoint is routes.rotate_credential]
    entries = [
        guards.GUARDED[d.call]
        for d in route.dependant.dependencies
        if d.call in guards.GUARDED and guards.GUARDED[d.call][0] is not None
    ]
    assert entries == [(Role.OPERATOR, "tenant_id", False)]
    assert guards.EMITTERS[routes.rotate_credential] is Action.CREDENTIAL_ROTATE
    assert guards.mutations_without_emitter(routes.router.routes) == []


async def test_a_rotation_returns_ids_and_audits_it_without_a_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The new secret reaches GitHub (the fake writer) and nowhere else."""
    caplog.set_level(logging.DEBUG)
    w, _runtime, writer, env, key = await _rotating()
    async with _client(env, ACME_OPERATOR) as client:
        response = await client.post(_path(w.tenant.id, "fake", key))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "delivered"
    assert body["operation_id"] is not None
    (event,) = writer.written
    assert (event.action, event.outcome, event.tenant_id) == (
        Action.CREDENTIAL_ROTATE,
        Outcome.SUCCESS,
        w.tenant.id,
    )
    assert (event.actor_sub, event.resource_type, event.resource_id) == ("u1", "credential", key)
    assert event.after == {
        "adapter": "fake",
        "operation_id": body["operation_id"],
        "operation_kind": "rotate",
    }
    secrets = [c["secret"] for c in w.platform.credentials.values()]
    assert len(secrets) == 2  # the overlap: old and new
    haystack = response.text + repr(writer.written) + caplog.text
    for value in secrets:
        assert value not in haystack


async def test_rotating_again_resumes_the_open_operation() -> None:
    """Idempotent: the same operation, one event per call."""
    w, _runtime, writer, env, key = await _rotating()
    async with _client(env, OPERATOR) as client:
        first = await client.post(_path(w.tenant.id, "fake", key))
        second = await client.post(_path(w.tenant.id, "fake", key))
    assert first.json()["operation_id"] == second.json()["operation_id"]
    assert len(writer.written) == 2


async def test_unknown_adapter_scope_and_malformed_path_parts() -> None:
    """404 for an adapter that is not enabled or a scope with no credential; 422 for bad shapes."""
    w, _runtime, writer, env, key = await _rotating()
    async with _client(env, OPERATOR) as client:
        problem(await client.post(_path(w.tenant.id, "nope", key)), 404, "not-found")
        missing = scope_key(PULL.repository_id, PULL.environment, PULL.permission)
        problem(await client.post(_path(w.tenant.id, "fake", missing)), 404, "not-found")
        problem(await client.post(_path(w.tenant.id, "Fake!", key)), 422, "validation")
        problem(await client.post(_path(w.tenant.id, "fake", "zz" + key)), 422, "validation")
        problem(await client.post(_path(w.tenant.id, "fake", "a" * 65)), 422, "validation")
        problem(await client.post(_path(w.tenant.id, "fake", key[:31])), 422, "validation")
    assert [e.outcome for e in writer.written] == [Outcome.FAILURE, Outcome.FAILURE]


async def test_roles_and_scopes() -> None:
    """A viewer is 403; an operator of another tenant 404; an instance operator passes."""
    w, _runtime, writer, env, key = await _rotating()
    async with _client(env, VIEWER) as client:
        problem(await client.post(_path(w.tenant.id, "fake", key)), 403, "forbidden")
    async with _client(env, OTHER_OPERATOR) as client:
        problem(await client.post(_path(w.tenant.id, "fake", key)), 404, "not-found")
    async with _client(env, OPERATOR) as client:
        assert (await client.post(_path(w.tenant.id, "fake", key))).status_code == 200
    assert len(writer.written) == 1


async def test_a_scope_with_nothing_to_rotate_is_a_409() -> None:
    """An OIDC scope has no secret to rotate."""
    w = build_world(fake_capabilities(oidc=True))
    await w.core.ensure_scope(w.tenant, "fake", PULL)
    runtime = CredentialRuntime(w.core, w.store, w.deps.tenants, frozenset({"fake"}))
    writer = Writer()
    env = _env(runtime, writer, w.tenant.id)
    key = scope_key(PULL.repository_id, PULL.environment, PULL.permission)
    async with _client(env, OPERATOR) as client:
        problem(await client.post(_path(w.tenant.id, "fake", key)), 409, "conflict")
    assert [e.outcome for e in writer.written] == [Outcome.FAILURE]


class Gate:
    """An `OperationGate` that refuses everything."""

    async def allows(self, tenant_id: UUID, kind: object) -> bool:
        """Never."""
        del tenant_id, kind
        return False


async def test_a_disabled_tenant_is_a_409_and_nothing_rotates() -> None:
    """The core's gate refuses the new operation."""
    w, _, _, _, key = await _rotating()
    gated = CredentialCore(replace(w.deps, gate=Gate()))
    runtime = CredentialRuntime(gated, w.store, w.deps.tenants, frozenset({"fake"}))
    writer = Writer()
    env = _env(runtime, writer, w.tenant.id)
    before = dict(w.platform.credentials)
    async with _client(env, OPERATOR) as client:
        problem(await client.post(_path(w.tenant.id, "fake", key)), 409, "conflict")
    assert w.platform.credentials == before
    assert [e.outcome for e in writer.written] == [Outcome.FAILURE]


class StubCore:
    """A core whose `rotate_scope` raises what a test asks for."""

    def __init__(self, error: Exception) -> None:
        """Raise `error` on every rotation."""
        self.error = error

    async def rotate_scope(self, *args: object) -> None:
        """Fail."""
        del args
        raise self.error


async def _stubbed(error: Exception, writer: Writer) -> tuple[Env, str, UUID]:
    w, _runtime, _w, _e, key = await _rotating()
    stub = cast(CredentialCore, StubCore(error))
    runtime = CredentialRuntime(
        stub,
        cast(CredentialStore, w.store),
        w.deps.tenants,
        frozenset({"fake"}),
    )
    return _env(runtime, writer, w.tenant.id), key, w.tenant.id


@pytest.mark.parametrize(
    ("error", "status", "kind"),
    [
        (RotationNotApplicableError(), 409, "conflict"),
        (LeaseBusy("scope"), 409, "conflict"),
        (LeaseLost(f"scope {CANARY}"), 409, "conflict"),
        (LeaseLostError(f"lost {CANARY}"), 409, "conflict"),
        (AdapterConfigurationError(f"no base_url {CANARY}"), 409, "conflict"),
        (TenantDisabledError(f"tenant {CANARY}"), 409, "conflict"),
        (UnknownTenantError(CANARY), 404, "not-found"),
        (RetryableOperationError(UUID(int=1), f"retry {CANARY}"), 502, "bad-gateway"),
        (OperationFailedError(UUID(int=1), f"failed {CANARY}"), 502, "bad-gateway"),
        (AdapterError(True, f"timeout {CANARY}"), 502, "bad-gateway"),
        (OutputError(f"GitHub 502 {CANARY}"), 502, "bad-gateway"),
        (GitHubApiError(502, f"bad gateway {CANARY}"), 502, "bad-gateway"),
        (HostNotReadyError(CANARY, HostStatus(HostState.UNAVAILABLE, CANARY)), 502, "bad-gateway"),
        (SecretUnavailable(f"store {CANARY}"), 502, "bad-gateway"),
    ],
)
async def test_each_failure_has_a_fixed_text_and_one_failure_event(
    error: Exception,
    status: int,
    kind: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The exception text, which may carry anything, reaches neither the answer nor the log."""
    caplog.set_level(logging.DEBUG)
    writer = Writer()
    env, key, tenant = await _stubbed(error, writer)
    async with _client(env, OPERATOR) as client:
        response = await client.post(_path(tenant, "fake", key))
    body = problem(response, status, kind)
    assert CANARY not in response.text + caplog.text + repr(writer.written)
    assert body["detail"] != str(error)
    (event,) = writer.written
    assert (event.action, event.outcome) == (Action.CREDENTIAL_ROTATE, Outcome.FAILURE)


async def test_an_unexpected_error_is_a_500_with_a_failure_event_and_a_broken_writer_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Whatever else happens is not hidden; a writer that fails too is logged."""
    writer = Writer()
    env, key, tenant = await _stubbed(RuntimeError(CANARY), writer)
    caplog.set_level(logging.DEBUG)
    async with _client(env, OPERATOR) as client:
        response = await client.post(_path(tenant, "fake", key))
        problem(response, 500)
        assert CANARY not in response.text + caplog.text
        assert [e.outcome for e in writer.written] == [Outcome.FAILURE]
        writer.error = OSError("audit down")
        problem(await client.post(_path(tenant, "fake", key)), 500)
    assert "could not record the failed credential.rotate event" in caplog.text


async def test_a_cancelled_rotation_still_leaves_its_failure_event() -> None:
    """A client that goes away mid-rotation does not make the call unaudited."""
    writer = Writer()
    env, key, tenant = await _stubbed(asyncio.CancelledError(), writer)
    async with _client(env, OPERATOR) as client:
        with pytest.raises(asyncio.CancelledError):
            await client.post(_path(tenant, "fake", key))
    assert [e.outcome for e in writer.written] == [Outcome.FAILURE]


async def test_without_a_credential_core_or_an_audit_writer_the_route_is_a_503() -> None:
    """Mock mode has neither; nothing rotates without a place to record it."""
    w, runtime, _writer, _e, key = await _rotating()
    for rt, writer in ((None, Writer()), (runtime, None)):
        env = _env(rt, writer, w.tenant.id)
        async with _client(env, OPERATOR) as client:
            problem(await client.post(_path(w.tenant.id, "fake", key)), 503, "unavailable")


async def test_a_success_event_that_cannot_be_written_fails_the_call() -> None:
    """The rotation stays resumable; the caller is told it did not complete cleanly."""
    w, _runtime, writer, env, key = await _rotating()
    writer.error = OSError("audit down")
    async with _client(env, OPERATOR) as client:
        problem(await client.post(_path(w.tenant.id, "fake", key)), 500)


async def test_a_rotation_that_adds_no_operation_id_is_still_audited() -> None:
    """`operation_id` is optional in the event."""
    w, runtime, writer, _e, key = await _rotating()

    class Quiet:
        async def rotate_scope(self, *args: object) -> Any:  # noqa: ANN401
            from trustbridge.adapters.core import ScopeResult  # noqa: PLC0415
            from trustbridge.adapters.interface import ChangeState  # noqa: PLC0415

            del args
            return ScopeResult(None, None, ChangeState.UNCHANGED)

    quiet = CredentialRuntime(
        cast(CredentialCore, Quiet()), w.store, w.deps.tenants, runtime.adapters
    )
    env = _env(quiet, writer, w.tenant.id)
    async with _client(env, OPERATOR) as client:
        response = await client.post(_path(w.tenant.id, "fake", key))
    assert response.json() == {
        "operation_id": None,
        "status": None,
        "change": "unchanged",
        "wait_until": None,
        "manual_action": None,
    }
    assert writer.written[0].after == {"adapter": "fake", "operation_kind": "rotate"}


def test_the_runtime_is_built_from_the_cores_parts_with_the_disable_gate() -> None:
    """The same parts the worker would use, the SQL store, and the gate; none without a database."""
    from types import SimpleNamespace  # noqa: PLC0415

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: PLC0415

    from trustbridge.adapters.sql_store import SqlCredentialStore  # noqa: PLC0415
    from trustbridge.tenancy.gate import SqlTenantGate  # noqa: PLC0415

    core: Any = SimpleNamespace(
        runtime=SimpleNamespace(gateway=object()),
        connections=object(),
        parked=object(),
        leases=object(),
        tenants=object(),
    )
    sessions = async_sessionmaker[AsyncSession]()
    writer = Writer()
    runtime = build_runtime(classes={}, core=core, sessions=sessions, writer=writer)
    assert runtime is not None
    assert isinstance(runtime.store, SqlCredentialStore)
    assert isinstance(runtime.core, CredentialCore)
    assert isinstance(runtime.core._d.gate, SqlTenantGate)
    assert runtime.adapters == frozenset()
    assert build_runtime(classes={}, core=core, sessions=None, writer=writer) is None
    assert build_runtime(classes={}, core=core, sessions=sessions, writer=None) is None
