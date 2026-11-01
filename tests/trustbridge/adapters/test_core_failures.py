"""Crash, retry, failure, limit, timeout and configuration behaviour of the core."""

import asyncio
from dataclasses import replace
from typing import ClassVar
from uuid import uuid4

import pytest
from pydantic import SecretStr

from tests.trustbridge.adapters.conftest import PULL, PUSH_PROD, World, build_world, make_tenant
from trustbridge.adapters.core import OperationFailedError, RetryableOperationError
from trustbridge.adapters.fake import FakeAdapter, FakePlatform, fake_capabilities
from trustbridge.adapters.interface import (
    AdapterConfigurationError,
    AdapterError,
    Change,
    ChangeState,
    CredentialOutcome,
    CredentialScope,
    CredentialState,
    IssuedSecret,
    OidcTrust,
    credential_name,
    scope_key,
)
from trustbridge.adapters.store import (
    CoreSettings,
    LeaseLostError,
    OperationStatus,
    OutputError,
    ScopeId,
)


def _key(scope: ScopeId) -> str:
    return scope_key(scope.repository_id, scope.environment, scope.permission)


async def test_lost_response_is_retried_without_a_second_credential(secret_world: World) -> None:
    """Invariants A2, A3, A8: the retry reuses the name and ends with one credential."""
    w = secret_world
    w.platform.fail_after["ensure_credential"] = [AdapterError(True, "connection reset")]
    with pytest.raises(RetryableOperationError):
        await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    assert len(w.platform.credentials) == 1
    assert w.writer.applied == []
    result = await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    assert result.status is OperationStatus.FINISHED
    assert list(w.platform.credentials) == [credential_name(w.tenant.id, _key(PUSH_PROD), 1)]
    assert len([o for o in w.writer.applied if o.kind == "secret"]) == 1


async def test_parked_values_are_delivered_without_calling_the_adapter_again(
    secret_world: World,
) -> None:
    """Invariant A8: once the values are parked, a delivery retry does not call the adapter."""
    w = secret_world
    w.writer.fail_next = [OutputError("GitHub 502")]
    with pytest.raises(RetryableOperationError):
        await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    assert len(w.values.parked) == 1
    issued_calls = w.platform.calls.count("ensure_credential")
    result = await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    assert result.status is OperationStatus.FINISHED
    assert w.platform.calls.count("ensure_credential") == issued_calls
    assert w.values.parked == {}


async def test_failed_initial_issue_leaves_no_platform_credential(secret_world: World) -> None:
    """Invariant A10: the credential created before the failure is deleted again."""
    w = secret_world
    w.platform.fail_after["ensure_credential"] = [AdapterError(False, "rejected")]
    with pytest.raises(OperationFailedError):
        await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    assert w.platform.credentials == {}
    stored = await w.store.get_credential(w.tenant.id, "fake", _key(PUSH_PROD))
    assert stored is not None
    assert stored.state.current is None
    ops = list(w.store.operations.values())
    assert [o.status for o in ops] == [OperationStatus.FAILED]
    assert w.values.parked == {}


async def test_exhausted_retries_fail_the_operation() -> None:
    """A retryable error becomes a failure after `max_attempts`."""
    w = build_world(fake_capabilities(oidc=False), settings=CoreSettings(max_attempts=2))
    w.platform.fail_before["ensure_credential"] = [
        AdapterError(True, "503"),
        AdapterError(True, "503"),
    ]
    with pytest.raises(RetryableOperationError):
        await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    with pytest.raises(OperationFailedError):
        await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)


async def test_failed_rotation_keeps_the_old_credential_and_deletes_the_new(
    secret_world: World,
) -> None:
    """Invariant A10: after a failed rotation the old credential is valid and the new gone."""
    w = secret_world
    await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    key = _key(PUSH_PROD)
    w.platform.fail_after["rotate_begin"] = [AdapterError(False, "quota")]
    with pytest.raises(OperationFailedError):
        await w.core.rotate_scope(w.tenant, "fake", PUSH_PROD)
    assert list(w.platform.credentials) == [credential_name(w.tenant.id, key, 1)]
    stored = await w.store.get_credential(w.tenant.id, "fake", key)
    assert stored is not None
    assert stored.kind == "secret"
    assert stored.state.current is not None
    assert stored.state.current.generation == 1
    assert stored.state.current.delivered is True
    assert stored.state.previous is None


async def test_manual_action_scope_does_not_block_other_scopes(secret_world: World) -> None:
    """Invariant A11: one scope parked at NEEDS_MANUAL_ACTION leaves the others alone."""
    w = secret_world
    w.platform.manual_action = "Create the connection in the UI"
    parked = await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    assert parked.change is ChangeState.NEEDS_MANUAL_ACTION
    assert parked.manual_action == "Create the connection in the UI"
    w.platform.manual_action = None
    other = await w.core.ensure_scope(w.tenant, "fake", replace(PUSH_PROD, permission="admin"))
    assert other.status is OperationStatus.FINISHED
    resumed = await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    assert resumed.status is OperationStatus.FINISHED


async def test_missing_privileged_credential_blocks_state_changes_only() -> None:
    """Invariant A13: no adapter call, a non-retryable error and a visible failed operation."""
    w = build_world(
        fake_capabilities(oidc=False, needs_privileged_credential=True), privileged=False
    )
    with pytest.raises(AdapterConfigurationError):
        await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    assert w.platform.calls == []
    assert w.writer.applied == []
    ops = list(w.store.operations.values())
    assert len(ops) == 1
    assert ops[0].status is OperationStatus.FAILED
    assert ops[0].error is not None
    with pytest.raises(AdapterConfigurationError):
        await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    assert len(w.store.operations) == 1


async def test_host_kind_outside_github_hosts_is_refused_before_any_call() -> None:
    """Invariant A7: the core checks the host kind first."""
    caps = replace(fake_capabilities(oidc=False), github_hosts=frozenset({"github.com"}))
    w = build_world(caps, tenant=make_tenant("ghes", id_claims=False))
    with pytest.raises(AdapterConfigurationError):
        await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    assert w.platform.calls == []


async def test_credential_limit_refuses_creation_and_rotation_past_the_limit() -> None:
    """0028-11: never more credentials than the limit; the old one stays valid."""
    w = build_world(fake_capabilities(oidc=False, credential_limit=2))
    await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    await w.core.ensure_scope(w.tenant, "fake", replace(PUSH_PROD, permission="admin"))
    with pytest.raises(AdapterError) as created:
        await w.core.ensure_scope(w.tenant, "fake", replace(PUSH_PROD, environment="x"))
    assert created.value.retryable is False
    with pytest.raises(AdapterError):
        await w.core.rotate_scope(w.tenant, "fake", PUSH_PROD)
    assert len(w.platform.credentials) == 2
    assert w.leases.held[-1][2] == "*"


async def test_adapter_call_is_bounded_by_a_timeout() -> None:
    """0028-11: a hanging adapter call ends in a (retryable) timeout error."""
    w = build_world(
        fake_capabilities(oidc=False),
        settings=CoreSettings(adapter_call_timeout_seconds=0.05, max_attempts=1),
    )
    w.platform.hang.add("ensure_credential")
    with pytest.raises(OperationFailedError):
        await asyncio.wait_for(w.core.ensure_scope(w.tenant, "fake", PUSH_PROD), timeout=5)


def test_call_timeout_must_be_shorter_than_the_lease_ttl() -> None:
    """0028-11: the settings refuse a timeout that is not shorter than the TTL."""
    with pytest.raises(ValueError, match="shorter"):
        CoreSettings(lease_ttl_seconds=30, adapter_call_timeout_seconds=30)


def _tampering_adapter(platform: FakePlatform, mode: str) -> type[FakeAdapter]:
    class Tampering(FakeAdapter):
        name: ClassVar[str] = "fake"
        _platform = platform
        _caps = fake_capabilities(oidc=True)

        async def ensure_credential(
            self, s: CredentialScope, prior: CredentialState
        ) -> CredentialOutcome:
            out = await super().ensure_credential(s, prior)
            if mode == "claims" and isinstance(out.credential, OidcTrust):
                bad = dict(out.credential.claims) | {"sub": "repo:acme/x"}
                return replace(out, credential=replace(out.credential, claims=bad))
            if mode == "unoffered":
                return CredentialOutcome(
                    Change(ChangeState.CREATED),
                    out.state,
                    OidcTrust("https://token.actions.githubusercontent.com", None, {}),
                )
            return out

    return Tampering


@pytest.mark.parametrize("mode", ["claims", "unoffered"])
async def test_oidc_claims_and_offer_are_enforced(mode: str) -> None:
    """Invariant A6: wrong claims, or trust for a scope not offered it, fail non-retryably."""
    platform = FakePlatform()
    scope = PULL if mode == "claims" else PUSH_PROD
    w = build_world(adapter_class=_tampering_adapter(platform, mode))
    with pytest.raises(OperationFailedError):
        await w.core.ensure_scope(w.tenant, "fake", scope)
    assert platform.credentials == {}


async def test_orphan_sweep_deletes_only_unaccounted_credentials(secret_world: World) -> None:
    """Invariant 22: a credential created after the platform list is never deleted."""
    w = secret_world
    await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    prefix = f"tb-{w.tenant.id.hex}-"
    w.platform.credentials[f"{prefix}deadbeefdeadbeef-g1"] = {
        "id": "x",
        "kind": "secret",
        "secret": "s",
    }
    w.platform.credentials["tb-" + uuid4().hex + "-other-g1"] = {
        "id": "y",
        "kind": "secret",
        "secret": "s",
    }

    async def create_after_list() -> None:
        w.platform.credentials[f"{prefix}latecomer-g1"] = {
            "id": "z",
            "kind": "secret",
            "secret": "",
        }

    w.store.before_known_names = create_after_list
    deleted = await w.core.sweep_orphans(w.tenant, "fake")
    assert deleted == [f"{prefix}deadbeefdeadbeef-g1"]
    assert f"{prefix}latecomer-g1" in w.platform.credentials
    assert len(w.platform.credentials) == 3


async def test_parked_values_of_terminal_or_missing_operations_are_swept(
    secret_world: World,
) -> None:
    """Invariant 23: the sweep removes values whose operation is terminal or unknown."""
    w = secret_world
    await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    finished = next(iter(w.store.operations))
    await w.values.put(finished, IssuedSecret({"token": SecretStr("a")}))
    await w.values.put(uuid4(), IssuedSecret({"token": SecretStr("b")}))
    assert await w.core.sweep_parked_values() == 2
    assert w.values.parked == {}


async def test_open_operations_are_resumed_by_the_reconciler(secret_world: World) -> None:
    """The reconciler's `resume_all` completes an operation left mid-way."""
    w = secret_world
    w.writer.fail_next = [OutputError("down")]
    with pytest.raises(RetryableOperationError):
        await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    results = await w.core.resume_all()
    assert [r.status for r in results] == [OperationStatus.FINISHED]
    assert await w.core.resume_all() == []


async def test_lost_lease_cannot_commit(secret_world: World) -> None:
    """Invariant 10 (in-memory form): a holder that lost its lease fails its guarded write."""
    w = secret_world

    async def steal_lease(method: str) -> None:
        del method
        for key in list(w.leases.fences):
            w.leases.fences[key] += 1

    w.platform.on_enter = steal_lease
    with pytest.raises(LeaseLostError):
        await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
