"""Eligibility rules in isolation, and the JSON codec of the persisted state."""

from datetime import UTC, datetime

from tests.trustbridge.adapters.conftest import FakeInspector, make_tenant
from trustbridge.adapters import codec
from trustbridge.adapters.eligibility import is_trust_eligible
from trustbridge.adapters.interface import (
    CredentialScope,
    CredentialSlot,
    CredentialState,
    Permission,
)
from trustbridge.adapters.store import DeliveredOutput, ScopeId


def _scope(repo: int | None, env: str | None, perm: Permission) -> CredentialScope:
    return CredentialScope.build(
        make_tenant(),
        repository_id=repo,
        repository_owner_id=1,
        environment=env,
        permission=perm,
    )


async def test_eligibility_matrix() -> None:
    """A12 as a table."""
    insp = FakeInspector()
    insp.protected.add((1, "prod"))
    assert await is_trust_eligible(_scope(None, None, "pull"), [], insp)
    assert await is_trust_eligible(_scope(1, None, "pull"), [], insp)
    assert await is_trust_eligible(_scope(1, "prod", "push"), [], insp)
    assert not await is_trust_eligible(_scope(1, "dev", "push"), [], insp)
    assert not await is_trust_eligible(_scope(1, None, "push"), [], insp)
    assert not await is_trust_eligible(_scope(None, None, "admin"), [], insp)
    assert not await is_trust_eligible(
        _scope(1, "prod", "push"), [_scope(1, "prod", "admin")], insp
    )
    assert await is_trust_eligible(_scope(1, "prod", "push"), [_scope(2, "prod", "admin")], insp)


def test_state_roundtrip_through_json() -> None:
    """The persisted JSON has no secret fields and decodes to the same state."""
    when = datetime(2026, 11, 1, tzinfo=UTC)
    state = CredentialState(
        CredentialSlot(2, "n2", {"id": "7"}, delivered=False),
        CredentialSlot(1, "n1", {"id": "6"}, delivered=True, retire_after=when),
    )
    assert codec.state_from_json(codec.state_to_json(state)) == state
    scope = ScopeId(3, 4, "prod", "push")
    assert codec.scope_from_json(codec.scope_to_json(scope)) == scope
    outs = (DeliveredOutput("secret", "A", 3, "prod"),)
    assert codec.outputs_from_json(codec.outputs_to_json(outs)) == outs
