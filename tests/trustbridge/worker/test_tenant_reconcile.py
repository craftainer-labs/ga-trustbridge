"""The tenant reconcile body over the credential core (NG5r-b), unit level with the fakes."""

import logging
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.trustbridge.adapters.conftest import PULL, PUSH_PROD, World, build_world
from trustbridge.adapters.core import (
    OperationFailedError,
    RetryableOperationError,
    TenantDisabledError,
)
from trustbridge.adapters.fake import fake_capabilities
from trustbridge.adapters.interface import AdapterError, IssuedSecret
from trustbridge.adapters.store import OperationStatus, OutputError
from trustbridge.worker.lease import LeaseBusy
from trustbridge.worker.reconcile_queue import ClaimLostError
from trustbridge.worker.runtime import Core
from trustbridge.worker.tenant_reconcile import (
    CredentialReconciler,
    ReconcileIncompleteError,
    SweepThrottle,
    build_tenant_reconciler,
)


class Extender:
    """An `Extend` that counts its calls and can lose the claim from a given call on."""

    def __init__(self, lose_at: int | None = None) -> None:
        """`lose_at` is the 1-based call that raises."""
        self.calls = 0
        self.lose_at = lose_at

    async def __call__(self) -> None:
        """Count, then maybe raise."""
        self.calls += 1
        if self.lose_at is not None and self.calls >= self.lose_at:
            raise ClaimLostError("gone")


def _body(world: World) -> CredentialReconciler:
    return CredentialReconciler(world.core, world.store, world.deps.tenants, ["fake"])


async def test_a_tenant_without_credentials_creates_nothing() -> None:
    """Option A: only what exists is reconciled; no scope is provisioned."""
    w = build_world(fake_capabilities(oidc=False))
    extend = Extender()
    await _body(w).reconcile(w.tenant.id, extend)
    assert w.store.credentials == {}
    assert w.platform.calls == []
    assert extend.calls == 1  # only before the parked-value sweep


async def test_eligibility_is_re_evaluated_for_each_stored_scope() -> None:
    """ADR 0029 A12: a scope that lost its protected environment is switched to a secret."""
    w = build_world(fake_capabilities(oidc=True))
    w.inspector.protected.add((10, "prod"))
    await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    [row] = await w.store.list_credentials(w.tenant.id, "fake")
    assert row.kind == "oidc"
    w.inspector.protected.clear()
    extend = Extender()
    await _body(w).reconcile(w.tenant.id, extend)
    [row] = await w.store.list_credentials(w.tenant.id, "fake")
    assert row.kind == "secret"
    assert extend.calls == 3  # ensure_scope, sweep_orphans, sweep_parked_values


async def test_the_stored_scopes_are_the_siblings() -> None:
    """Scopes sharing an environment across permissions lose their trust together."""
    w = build_world(fake_capabilities(oidc=True))
    w.inspector.protected.add((10, "prod"))
    await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    other = replace(PUSH_PROD, permission="admin")
    await w.core.ensure_scope(w.tenant, "fake", other, [PUSH_PROD])  # shared: a secret
    await _body(w).reconcile(w.tenant.id, Extender())
    kinds = {
        c.scope.permission: c.kind for c in await w.store.list_credentials(w.tenant.id, "fake")
    }
    assert kinds == {"push": "secret", "admin": "secret"}


async def test_an_open_operation_is_resumed_and_a_second_run_changes_nothing() -> None:
    """The write-ahead operation is finished; a repeat run is a no-op (idempotent)."""
    w = build_world(fake_capabilities(oidc=False))
    w.writer.fail_next = [OutputError("down")]
    with pytest.raises(RetryableOperationError):
        await w.core.ensure_scope(w.tenant, "fake", PULL)
    foreign = next(iter(w.store.operations.values()))
    other_tenant_op = replace(foreign, id=uuid4(), tenant_id=uuid4(), scope_key="other")
    w.store.operations[other_tenant_op.id] = other_tenant_op
    await _body(w).reconcile(w.tenant.id, Extender())
    assert w.store.operations[foreign.id].status is OperationStatus.FINISHED
    assert w.store.operations[other_tenant_op.id] == other_tenant_op  # not this tenant's
    names = dict(w.platform.credentials)
    credentials = dict(w.store.credentials)
    await _body(w).reconcile(w.tenant.id, Extender())
    assert w.platform.credentials == names
    assert w.store.credentials == credentials


async def test_orphans_and_parked_values_are_swept() -> None:
    """The platform credential nothing accounts for and the value of a missing operation go."""
    w = build_world(fake_capabilities(oidc=False))
    await w.core.ensure_scope(w.tenant, "fake", PULL)
    orphan = f"tb-{w.tenant.id.hex}-deadbeefdeadbeef-g1"
    w.platform.credentials[orphan] = {"id": "x", "kind": "secret", "secret": "s"}
    await w.values.put(uuid4(), IssuedSecret({"token": SecretStr("b")}))
    await _body(w).reconcile(w.tenant.id, Extender())
    assert orphan not in w.platform.credentials
    assert w.values.parked == {}


async def test_a_lost_claim_aborts_before_the_next_write_step() -> None:
    """`extend` raising stops the body at once: the step after it never runs."""
    w = build_world(fake_capabilities(oidc=False))
    w.writer.fail_next = [OutputError("down")]
    with pytest.raises(RetryableOperationError):
        await w.core.ensure_scope(w.tenant, "fake", PULL)
    calls = list(w.platform.calls)
    with pytest.raises(ClaimLostError):
        await _body(w).reconcile(w.tenant.id, Extender(lose_at=1))  # before resuming
    assert w.platform.calls == calls
    assert not any(op.terminal for op in w.store.operations.values())
    # lost between the steps: the resume ran, the scope step did not
    ensure = Extender(lose_at=2)
    with pytest.raises(ClaimLostError):
        await _body(w).reconcile(w.tenant.id, ensure)
    assert all(op.terminal for op in w.store.operations.values())
    assert ensure.calls == 2


class StubCore:
    """A core whose steps record themselves and fail on request."""

    def __init__(self, **errors: Exception) -> None:
        """`errors` maps a step name to what it raises."""
        self.errors = errors
        self.steps: list[str] = []

    async def _step(self, name: str) -> None:
        self.steps.append(name)
        if name in self.errors:
            raise self.errors[name]

    async def resume_operation(self, operation_id: UUID) -> None:
        """Resume."""
        del operation_id
        await self._step("resume")

    async def ensure_scope(self, *args: object) -> None:
        """Ensure."""
        del args
        await self._step("ensure")

    async def sweep_orphans(self, *args: object) -> list[str]:
        """Sweep."""
        del args
        await self._step("orphans")
        return []

    async def sweep_parked_values(self) -> int:
        """Sweep."""
        await self._step("parked")
        return 0


async def _stubbed(world: World, core: StubCore) -> CredentialReconciler:
    await world.core.ensure_scope(world.tenant, "fake", PULL)
    op = next(iter(world.store.operations.values()))
    world.store.operations[op.id] = replace(op, status=OperationStatus.INTENT)
    return CredentialReconciler(
        cast("Any", core), world.store, world.deps.tenants, ["fake", "empty"]
    )


@pytest.mark.parametrize(
    "error",
    [
        RetryableOperationError(uuid4(), "retry"),
        OperationFailedError(uuid4(), "failed"),
        AdapterError(True, "platform"),
        LeaseBusy("fake/k"),
    ],
)
async def test_a_failing_step_does_not_stop_the_others_but_fails_the_run(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    """Every step runs; the failures are counted and reported at the end, by type only."""
    w = build_world(fake_capabilities(oidc=False))
    core = StubCore(resume=error, ensure=error, orphans=error, parked=error)
    body = await _stubbed(w, core)
    with caplog.at_level(logging.WARNING), pytest.raises(ReconcileIncompleteError, match="3 "):
        await body.reconcile(w.tenant.id, Extender())
    assert core.steps == ["resume", "ensure", "orphans", "parked"]
    assert caplog.text.count("tenant reconcile step failed") == 4
    assert type(error).__name__ in caplog.text


async def test_a_tenant_disabled_mid_run_stops_quietly(caplog: pytest.LogCaptureFixture) -> None:
    """Invariant 19 on the core's gate: the body ends, the request is then acknowledged."""
    w = build_world(fake_capabilities(oidc=False))
    core = StubCore(ensure=TenantDisabledError())
    body = await _stubbed(w, core)
    with caplog.at_level(logging.INFO):
        await body.reconcile(w.tenant.id, Extender())
    assert core.steps == ["resume", "ensure"]
    assert "tenant reconcile stopped: tenant is disabled" in caplog.text


async def test_an_unexpected_error_is_not_swallowed() -> None:
    """Only the platform, GitHub and lease errors are counted; a bug propagates."""
    w = build_world(fake_capabilities(oidc=False))
    body = await _stubbed(w, StubCore(ensure=RuntimeError("bug")))
    with pytest.raises(RuntimeError, match="bug"):
        await body.reconcile(w.tenant.id, Extender())


def test_the_factory_composes_the_core_for_the_reconciler_only_with_a_database() -> None:
    """No session factory, no body; with one, the body over the enabled adapters (sorted)."""
    parts = SimpleNamespace(
        runtime=SimpleNamespace(gateway=object()),
        connections=object(),
        parked=object(),
        leases=object(),
        tenants=object(),
    )
    core = cast("Core", parts)
    classes: dict[str, Any] = {"b": object, "a": object}
    assert build_tenant_reconciler(core, None, classes) is None
    sessions = cast("async_sessionmaker[AsyncSession]", object())
    body = build_tenant_reconciler(core, sessions, classes)
    assert isinstance(body, CredentialReconciler)
    assert body._adapters == ("a", "b")


async def test_the_global_parked_sweep_runs_once_per_interval() -> None:
    """Many requests in a minute cost one sweep; a failed sweep is not counted as done."""
    w = build_world(fake_capabilities(oidc=False))
    core = StubCore()
    await _stubbed(w, core)
    now = [0.0]
    body = CredentialReconciler(
        cast("Any", core),
        w.store,
        w.deps.tenants,
        ["fake"],
        sweep=SweepThrottle(60.0, lambda: now[0]),
    )
    for at in (0.0, 10.0, 59.0, 61.0):
        now[0] = at
        await body.reconcile(w.tenant.id, Extender())
    assert core.steps.count("parked") == 2


async def test_a_failing_parked_sweep_is_logged_retried_and_does_not_fail_the_tenant() -> None:
    """The sweep is the instance's housekeeping: not marked done, so the next request retries."""
    w = build_world(fake_capabilities(oidc=False))
    core = StubCore(parked=AdapterError(True, "store down"))
    await _stubbed(w, core)
    body = CredentialReconciler(cast("Any", core), w.store, w.deps.tenants, ["fake"])
    await body.reconcile(w.tenant.id, Extender())  # no ReconcileIncompleteError
    await body.reconcile(w.tenant.id, Extender())
    assert core.steps.count("parked") == 2  # retried at once, not throttled
