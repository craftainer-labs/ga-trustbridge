"""A disabled tenant starts no new adapter operation (ADR 0033 invariant 19)."""

from dataclasses import replace
from uuid import UUID

import pytest

from tests.trustbridge.adapters.conftest import PULL, PUSH_PROD, World, build_world
from trustbridge.adapters.core import CredentialCore, TenantDisabledError
from trustbridge.adapters.fake import fake_capabilities
from trustbridge.adapters.store import OperationKind, OperationStatus


class Gate:
    """An `OperationGate` that admits or refuses everything and remembers what it was asked."""

    def __init__(self, *, open_: bool) -> None:
        """Start open or closed."""
        self.open = open_
        self.asked: list[tuple[UUID, OperationKind | None]] = []

    async def allows(self, tenant_id: UUID, kind: OperationKind | None) -> bool:
        """Record the question and answer."""
        self.asked.append((tenant_id, kind))
        return self.open


def _gated(world: World, gate: Gate) -> CredentialCore:
    return CredentialCore(replace(world.deps, gate=gate))


async def test_a_disabled_tenant_issues_nothing(secret_world: World) -> None:
    """No operation record, no platform call, no output."""
    w = secret_world
    core = _gated(w, Gate(open_=False))
    with pytest.raises(TenantDisabledError):
        await core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    assert w.platform.credentials == {}
    assert w.writer.applied == []


async def test_the_gate_is_asked_with_the_operation_kind(secret_world: World) -> None:
    """NG6h's own-operations exception keys on the kind."""
    w = secret_world
    gate = Gate(open_=True)
    core = _gated(w, gate)
    await core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    assert gate.asked == [(w.tenant.id, OperationKind.ISSUE)]


async def test_an_in_flight_operation_finishes_after_the_tenant_is_disabled(
    secret_world: World,
) -> None:
    """Only a new operation is refused; resuming the open rotation still works."""
    w = secret_world
    gate = Gate(open_=True)
    core = _gated(w, gate)
    await core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    started = await core.rotate_scope(w.tenant, "fake", PUSH_PROD)
    assert started.status is OperationStatus.DELIVERED
    gate.open = False
    w.clock.advance(3601)
    finished = await core.rotate_scope(w.tenant, "fake", PUSH_PROD)
    assert finished.status is OperationStatus.FINISHED
    with pytest.raises(TenantDisabledError):
        await core.rotate_scope(w.tenant, "fake", PUSH_PROD)


async def test_a_refresh_and_an_orphan_sweep_are_refused(oidc_world: World) -> None:
    """Both call the adapter, so both are gated (the kind is `None`)."""
    w = oidc_world
    gate = Gate(open_=True)
    core = _gated(w, gate)
    await core.ensure_scope(w.tenant, "fake", PULL)
    gate.open = False
    with pytest.raises(TenantDisabledError):
        await core.ensure_scope(w.tenant, "fake", PULL)
    with pytest.raises(TenantDisabledError):
        await core.sweep_orphans(w.tenant, "fake")
    assert gate.asked[-2:] == [(w.tenant.id, None), (w.tenant.id, None)]


async def test_without_a_gate_nothing_changes() -> None:
    """The default is no gate."""
    w = build_world(fake_capabilities(oidc=False))
    result = await w.core.ensure_scope(w.tenant, "fake", PULL)
    assert result.status is OperationStatus.FINISHED
