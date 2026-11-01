"""Credential operations through the core: issue, OIDC, rotation, switching, deletion."""

from dataclasses import replace
from datetime import timedelta

from pydantic import SecretStr

from tests.trustbridge.adapters.conftest import (
    ORG_PUSH,
    PULL,
    PUSH_PROD,
    World,
    build_world,
    make_tenant,
)
from trustbridge.adapters.fake import fake_capabilities
from trustbridge.adapters.interface import ChangeState, credential_name, scope_key
from trustbridge.adapters.store import OperationStatus, ScopeId


def _name(world: World, scope_id_key: str, generation: int) -> str:
    return credential_name(world.tenant.id, scope_id_key, generation)


def _key(scope: ScopeId) -> str:
    return scope_key(scope.repository_id, scope.environment, scope.permission)


async def test_issue_secret_parks_delivers_and_unparks(secret_world: World) -> None:
    """Invariants A8 and 0028-23: the value is parked, delivered, then removed."""
    w = secret_world
    result = await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    assert result.status is OperationStatus.FINISHED
    key = _key(PUSH_PROD)
    stored = await w.store.get_credential(w.tenant.id, "fake", key)
    assert stored is not None
    assert stored.kind == "secret"
    assert stored.state.current is not None
    assert stored.state.current.delivered is True
    assert stored.state.current.name == _name(w, key, 1)
    assert list(w.platform.credentials) == [_name(w, key, 1)]
    secrets = [o for o in w.writer.applied if o.kind == "secret"]
    assert len(secrets) == 1
    assert isinstance(secrets[0].value, SecretStr)
    assert secrets[0].value.get_secret_value() == w.platform.credentials[_name(w, key, 1)]["secret"]
    assert w.values.parked == {}


async def test_second_reconcile_of_a_delivered_secret_is_unchanged(secret_world: World) -> None:
    """A delivered secret is not re-issued or re-delivered."""
    w = secret_world
    await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    calls, applied = list(w.platform.calls), len(w.writer.applied)
    again = await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    assert again.change is ChangeState.UNCHANGED
    assert w.platform.calls == calls
    assert len(w.writer.applied) == applied


async def test_oidc_scope_rederives_outputs_on_every_reconcile(oidc_world: World) -> None:
    """Invariant A1 and 0028-21: trust is returned again and the outputs rewritten."""
    w = oidc_world
    await w.core.ensure_scope(w.tenant, "fake", PULL)
    key = _key(PULL)
    stored = await w.store.get_credential(w.tenant.id, "fake", key)
    assert stored is not None
    assert stored.kind == "oidc"
    assert w.platform.credentials[_name(w, key, 1)]["kind"] == "trust"
    assert [o.kind for o in w.writer.applied] == ["variable"]
    again = await w.core.ensure_scope(w.tenant, "fake", PULL)
    assert again.change is ChangeState.UNCHANGED
    assert len(w.writer.applied) == 2
    assert len(w.platform.credentials) == 1


async def test_no_oidc_without_id_claims_on_the_host() -> None:
    """Invariants A6 and A7 (0030-6): a host without id claims is never offered trust."""
    w = build_world(fake_capabilities(oidc=True), tenant=make_tenant("ghes", id_claims=False))
    await w.core.ensure_scope(w.tenant, "fake", PULL)
    stored = await w.store.get_credential(w.tenant.id, "fake", _key(PULL))
    assert stored is not None
    assert stored.kind == "secret"


async def test_push_scope_uses_trust_only_with_a_distinct_protected_environment(
    oidc_world: World,
) -> None:
    """Invariant A12: org-wide push never, unprotected never, protected and unshared yes."""
    w = oidc_world
    await w.core.ensure_scope(w.tenant, "fake", ORG_PUSH)
    org = await w.store.get_credential(w.tenant.id, "fake", _key(ORG_PUSH))
    assert org is not None
    assert org.kind == "secret"
    await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    unprotected = await w.store.get_credential(w.tenant.id, "fake", _key(PUSH_PROD))
    assert unprotected is not None
    assert unprotected.kind == "secret"
    w.inspector.protected.add((10, "prod"))
    other = replace(PUSH_PROD, environment="stage")
    w.inspector.protected.add((10, "stage"))
    await w.core.delete_scope(w.tenant, "fake", PUSH_PROD)
    await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD, siblings=[other])
    trusted = await w.store.get_credential(w.tenant.id, "fake", _key(PUSH_PROD))
    assert trusted is not None
    assert trusted.kind == "oidc"


async def test_shared_environment_is_not_eligible(oidc_world: World) -> None:
    """Invariant A12: an environment used by another permission cannot carry push trust."""
    w = oidc_world
    w.inspector.protected.add((10, "prod"))
    admin = replace(PUSH_PROD, permission="admin")
    await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD, siblings=[admin])
    stored = await w.store.get_credential(w.tenant.id, "fake", _key(PUSH_PROD))
    assert stored is not None
    assert stored.kind == "secret"


async def test_scope_losing_eligibility_is_switched_to_a_secret(oidc_world: World) -> None:
    """Invariant A12: the next reconcile issues a secret and revokes the trust at once."""
    w = oidc_world
    w.inspector.protected.add((10, "prod"))
    await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    key = _key(PUSH_PROD)
    first = await w.store.get_credential(w.tenant.id, "fake", key)
    assert first is not None
    assert first.kind == "oidc"
    w.inspector.protected.clear()
    result = await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    assert result.status is OperationStatus.FINISHED
    now = await w.store.get_credential(w.tenant.id, "fake", key)
    assert now is not None
    assert now.kind == "secret"
    assert now.state.previous is None
    assert list(w.platform.credentials) == [_name(w, key, 2)]
    assert w.platform.credentials[_name(w, key, 2)]["kind"] == "secret"


async def test_scope_gaining_eligibility_moves_to_trust_after_the_grace_period(
    oidc_world: World,
) -> None:
    """Secret to trust: the old secret is deleted only once `retire_after` has passed."""
    w = oidc_world
    await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    key = _key(PUSH_PROD)
    w.inspector.protected.add((10, "prod"))
    waiting = await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    assert waiting.status is OperationStatus.DELIVERED
    assert waiting.wait_until is not None
    assert len(w.platform.credentials) == 2
    w.clock.advance(3601)
    done = await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    assert done.status is OperationStatus.FINISHED
    assert list(w.platform.credentials) == [_name(w, key, 2)]


async def test_two_phase_rotation_overlaps_then_finishes(secret_world: World) -> None:
    """Invariant A9: begin, deliver, retire_after, then rotate_finish deletes only previous."""
    w = secret_world
    await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    key = _key(PUSH_PROD)
    started = await w.core.rotate_scope(w.tenant, "fake", PUSH_PROD)
    assert started.status is OperationStatus.DELIVERED
    assert set(w.platform.credentials) == {_name(w, key, 1), _name(w, key, 2)}
    stored = await w.store.get_credential(w.tenant.id, "fake", key)
    assert stored is not None
    assert stored.state.previous is not None
    assert stored.state.previous.retire_after == w.clock.now + timedelta(seconds=3600)
    # A rotation request while one is in flight resumes it: no third generation.
    again = await w.core.rotate_scope(w.tenant, "fake", PUSH_PROD)
    assert again.operation_id == started.operation_id
    assert set(w.platform.credentials) == {_name(w, key, 1), _name(w, key, 2)}
    w.clock.advance(3601)
    finished = await w.core.rotate_scope(w.tenant, "fake", PUSH_PROD)
    assert finished.status is OperationStatus.FINISHED
    assert list(w.platform.credentials) == [_name(w, key, 2)]
    final = await w.store.get_credential(w.tenant.id, "fake", key)
    assert final is not None
    assert final.state.previous is None
    assert final.state.current is not None
    assert final.state.current.generation == 2


async def test_single_rotation_replaces_in_place() -> None:
    """`rotation="single"`: the same name, no previous, no finish."""
    w = build_world(fake_capabilities(oidc=False, rotation="single"))
    await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    before = dict(next(iter(w.platform.credentials.values())))
    result = await w.core.rotate_scope(w.tenant, "fake", PUSH_PROD)
    assert result.status is OperationStatus.FINISHED
    assert len(w.platform.credentials) == 1
    assert next(iter(w.platform.credentials.values()))["secret"] != before["secret"]


async def test_delete_scope_removes_platform_credential_and_outputs(secret_world: World) -> None:
    """Deleting a scope deletes the credential and the recorded GitHub outputs."""
    w = secret_world
    await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    result = await w.core.delete_scope(w.tenant, "fake", PUSH_PROD)
    assert result.change is ChangeState.DELETED
    assert w.platform.credentials == {}
    assert {o.name for o in w.writer.removed} == {"FAKE_REGISTRY", "FAKE_TOKEN"}
