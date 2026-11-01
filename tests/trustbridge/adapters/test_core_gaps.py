"""Core paths the main scenarios do not reach: refresh, resume, rotation wait, cleanup, sweeps."""

from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from pydantic import SecretStr

from tests.trustbridge.adapters.conftest import PULL, PUSH_PROD, World, build_world
from trustbridge.adapters.core import (
    OperationFailedError,
    RetryableOperationError,
    RotationNotApplicableError,
    _with_current_delivered,
    expected_claims,
)
from trustbridge.adapters.fake import FakePlatform, fake_capabilities, make_fake_adapter
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
    Operation,
    OperationKind,
    OperationStatus,
    OutputError,
    ScopeId,
)

FAST = CoreSettings(lease_ttl_seconds=1, adapter_call_timeout_seconds=0.01)


def _key(scope: ScopeId) -> str:
    return scope_key(scope.repository_id, scope.environment, scope.permission)


async def _stuck_parked(w: World) -> Operation:
    """Leave a secret operation parked: values issued, delivery to GitHub failing once."""
    w.writer.fail_next = [OutputError("GitHub 502")]
    with pytest.raises(RetryableOperationError):
        await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    return next(o for o in w.store.operations.values() if o.status is OperationStatus.PARKED)


async def test_an_adapter_that_is_not_enabled_is_a_configuration_error(secret_world: World) -> None:
    """The registry refuses an unknown adapter name."""
    with pytest.raises(AdapterConfigurationError, match="not enabled"):
        await secret_world.core.ensure_scope(secret_world.tenant, "nope", PULL)


def test_org_wide_trust_claims_carry_only_the_owner(secret_world: World) -> None:
    """Without a repository the required claims are the owner id alone."""
    scope = CredentialScope.build(
        secret_world.tenant,
        repository_id=None,
        repository_owner_id=5,
        environment=None,
        permission="push",
    )
    assert expected_claims(scope) == {"repository_owner_id": "5"}


def test_marking_an_empty_state_delivered_changes_nothing() -> None:
    """A state without a current slot is returned as is."""
    state = CredentialState(current=None)
    assert _with_current_delivered(state) is state


async def test_rotation_needs_an_issued_secret_and_delete_of_nothing_is_unchanged(
    secret_world: World,
) -> None:
    """A never-issued scope cannot rotate, and deleting it reports nothing to do."""
    w = secret_world
    with pytest.raises(RotationNotApplicableError):
        await w.core.rotate_scope(w.tenant, "fake", PULL)
    result = await w.core.delete_scope(w.tenant, "fake", PULL)
    assert result.change is ChangeState.UNCHANGED


async def test_delete_waits_for_a_rotation_that_has_not_finished(secret_world: World) -> None:
    """An open two-phase rotation is advanced first; while it waits, delete returns its state."""
    w = secret_world
    await w.core.ensure_scope(w.tenant, "fake", PULL)
    rotating = await w.core.rotate_scope(w.tenant, "fake", PULL)
    assert rotating.status is OperationStatus.DELIVERED
    result = await w.core.delete_scope(w.tenant, "fake", PULL)
    assert result.status is OperationStatus.DELIVERED
    w.clock.advance(3601)
    deleted = await w.core.delete_scope(w.tenant, "fake", PULL)
    assert deleted.change is ChangeState.DELETED


async def test_resume_operation_ignores_missing_unowned_and_just_finished_operations(
    secret_world: World,
) -> None:
    """Nothing to resume: unknown id, no credential row, or finished while the lease was taken."""
    w = secret_world
    assert await w.core.resume_operation(uuid4()) is None
    orphan = Operation(
        id=uuid4(),
        tenant_id=w.tenant.id,
        adapter="fake",
        scope_key="k",
        kind=OperationKind.ISSUE,
        status=OperationStatus.INTENT,
        oidc_offered=False,
        base_state=None,
        base_kind=None,
    )
    w.store.operations[orphan.id] = orphan
    assert await w.core.resume_operation(orphan.id) is None  # no credential row
    stuck = await _stuck_parked(w)
    real_get = w.store.get_operation
    calls = {"n": 0}

    async def finishing(operation_id):  # type: ignore[no-untyped-def]  # noqa: ANN001, ANN202
        calls["n"] += 1
        op = await real_get(operation_id)
        if op is not None and calls["n"] > 1:
            return replace(op, status=OperationStatus.FINISHED)
        return op

    w.store.get_operation = finishing  # type: ignore[method-assign]
    assert await w.core.resume_operation(stuck.id) is None


async def test_resume_all_continues_after_a_failure_and_skips_finished_operations(
    secret_world: World,
) -> None:
    """A failing resume is logged and skipped; a terminal operation yields no result."""
    w = secret_world
    stuck = await _stuck_parked(w)
    w.writer.fail_next = [OutputError("still down")]
    assert await w.core.resume_all() == []
    assert await w.core.sweep_parked_values() == 0  # the open operation keeps its values
    assert stuck.id in w.values.parked

    async def unknown_operation() -> list[Operation]:
        return [replace(stuck, id=uuid4())]

    w.store.list_open_operations = unknown_operation  # type: ignore[method-assign]
    assert await w.core.resume_all() == []


async def test_lost_parked_values_are_issued_again_under_the_same_name(secret_world: World) -> None:
    """A wiped value store makes the next call re-issue instead of delivering nothing."""
    w = secret_world
    await _stuck_parked(w)
    w.values.parked.clear()
    result = await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    assert result.status is OperationStatus.FINISHED


async def test_an_unparkable_cleanup_failure_is_only_logged(secret_world: World) -> None:
    """Removing parked values may fail; the operation still finishes."""
    w = secret_world

    async def boom(operation_id):  # type: ignore[no-untyped-def]  # noqa: ANN001, ANN202
        raise ConnectionError("secret store down")

    w.values.delete = boom  # type: ignore[method-assign]
    result = await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    assert result.status is OperationStatus.FINISHED


async def test_refresh_of_a_trust_scope_handles_timeouts_manual_steps_and_write_failures() -> None:
    """Re-ensuring an OIDC scope: timeout, manual action and a failing GitHub write."""
    w = build_world(fake_capabilities(oidc=True), settings=FAST)
    first = await w.core.ensure_scope(w.tenant, "fake", PULL)
    assert first.status is OperationStatus.FINISHED
    w.platform.hang.add("ensure_credential")
    with pytest.raises(AdapterError, match="timed out"):
        await w.core.ensure_scope(w.tenant, "fake", PULL)
    w.platform.hang.clear()
    w.platform.manual_action = "approve the trust in the UI"
    manual = await w.core.ensure_scope(w.tenant, "fake", PULL)
    assert manual.change is ChangeState.NEEDS_MANUAL_ACTION
    w.platform.manual_action = None
    w.writer.fail_next = [OutputError("GitHub 502")]
    with pytest.raises(AdapterError, match="502"):
        await w.core.ensure_scope(w.tenant, "fake", PULL)


def _adapter(platform: FakePlatform, wrong: str) -> type:
    base: Any = make_fake_adapter(platform, fake_capabilities(oidc=True))

    class Odd(base):  # type: ignore[misc]
        async def ensure_credential(
            self, s: CredentialScope, prior: CredentialState
        ) -> CredentialOutcome:
            if wrong == "secret-on-refresh" and prior.current and prior.current.delivered:
                return CredentialOutcome(
                    Change(ChangeState.UPDATED), prior, IssuedSecret({"t": SecretStr("v")})
                )
            if wrong == "no-credential":
                return CredentialOutcome(Change(ChangeState.UNCHANGED), prior, None)
            outcome = await super().ensure_credential(s, prior)
            if wrong == "issuer" and isinstance(outcome.credential, OidcTrust):
                trust = replace(outcome.credential, issuer="https://evil.example")
                return replace(outcome, credential=trust)
            return outcome

    return Odd


async def test_adapters_returning_the_wrong_thing_are_refused() -> None:
    """A secret during a refresh, a foreign issuer and a missing credential are all errors."""
    refresh = build_world(adapter_class=_adapter(FakePlatform(), "secret-on-refresh"))
    await refresh.core.ensure_scope(refresh.tenant, "fake", PULL)
    with pytest.raises(AdapterError, match="refresh of a non-secret scope"):
        await refresh.core.ensure_scope(refresh.tenant, "fake", PULL)
    for wrong, text in (("issuer", "issuer differs"), ("no-credential", "no credential")):
        w = build_world(adapter_class=_adapter(FakePlatform(), wrong))
        with pytest.raises(OperationFailedError, match=text):
            await w.core.ensure_scope(w.tenant, "fake", PULL)


async def test_an_operation_left_in_cleanup_is_completed_on_the_next_call(
    secret_world: World,
) -> None:
    """A crash during cleanup resumes at the cleanup step."""
    w = secret_world
    stuck = await _stuck_parked(w)
    w.store.operations[stuck.id] = replace(stuck, status=OperationStatus.CLEANUP)
    result = await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    assert result.status is OperationStatus.FAILED
    assert w.store.operations[stuck.id].status is OperationStatus.FAILED


async def test_a_delivered_operation_without_a_previous_credential_finishes(
    secret_world: World,
) -> None:
    """Nothing to retire: the delivered step just finishes."""
    w = secret_world
    await w.core.ensure_scope(w.tenant, "fake", PULL)
    key = _key(PULL)
    op = Operation(
        id=uuid4(),
        tenant_id=w.tenant.id,
        adapter="fake",
        scope_key=key,
        kind=OperationKind.ISSUE,
        status=OperationStatus.DELIVERED,
        oidc_offered=False,
        base_state=None,
        base_kind=None,
    )
    w.store.operations[op.id] = op
    result = await w.core.ensure_scope(w.tenant, "fake", PULL)
    assert result.status is OperationStatus.FINISHED


async def test_retiring_the_previous_credential_can_fail_or_time_out() -> None:
    """Both failures leave the rotation open for a retry."""
    w = build_world(fake_capabilities(oidc=False), settings=FAST)
    await w.core.ensure_scope(w.tenant, "fake", PULL)
    await w.core.rotate_scope(w.tenant, "fake", PULL)
    w.clock.advance(3601)
    w.platform.fail_before["rotate_finish"] = [AdapterError(True, "platform 503")]
    with pytest.raises(RetryableOperationError):
        await w.core.ensure_scope(w.tenant, "fake", PULL)
    w.platform.hang.add("rotate_finish")
    with pytest.raises(RetryableOperationError):
        await w.core.ensure_scope(w.tenant, "fake", PULL)
    w.platform.hang.clear()
    done = await w.core.ensure_scope(w.tenant, "fake", PULL)
    assert done.status is OperationStatus.FINISHED


async def test_a_failing_output_removal_keeps_a_delete_open(secret_world: World) -> None:
    """GitHub refusing to remove outputs is retried, not forgotten."""
    w = secret_world
    await w.core.ensure_scope(w.tenant, "fake", PULL)
    w.writer.fail_remove = [OutputError("GitHub 502")]
    with pytest.raises(RetryableOperationError):
        await w.core.delete_scope(w.tenant, "fake", PULL)


async def test_cleanup_that_cannot_delete_abandons_the_operation() -> None:
    """A rejected issue whose cleanup also fails ends abandoned; a hung cleanup is retried."""
    w = build_world(fake_capabilities(oidc=False), settings=FAST)
    w.platform.fail_before["ensure_credential"] = [AdapterError(False, "rejected")]
    w.platform.fail_before["delete_credential"] = [AdapterError(False, "cannot delete")]
    with pytest.raises(OperationFailedError):
        await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    assert any(o.status is OperationStatus.ABANDONED for o in w.store.operations.values())
    hung = build_world(fake_capabilities(oidc=False), settings=FAST)
    hung.platform.fail_before["ensure_credential"] = [AdapterError(False, "rejected")]
    hung.platform.hang.add("delete_credential")
    with pytest.raises(RetryableOperationError):
        await hung.core.ensure_scope(hung.tenant, "fake", PUSH_PROD)


async def test_a_failed_delete_restores_the_credential_without_calling_the_platform_again(
    secret_world: World,
) -> None:
    """Delete has no new credential to clean up, so a refused delete simply fails."""
    w = secret_world
    await w.core.ensure_scope(w.tenant, "fake", PULL)
    w.platform.fail_before["delete_credential"] = [AdapterError(False, "forbidden")]
    with pytest.raises(OperationFailedError):
        await w.core.delete_scope(w.tenant, "fake", PULL)
    assert w.platform.calls.count("delete_credential") == 1


async def test_the_orphan_sweep_removes_only_unaccounted_credentials(secret_world: World) -> None:
    """A platform credential with our prefix that no row knows is deleted."""
    w = secret_world
    await w.core.ensure_scope(w.tenant, "fake", PULL)
    orphan = credential_name(w.tenant.id, "ab" * 8, 1)
    w.platform.credentials[orphan] = {"id": "x", "kind": "secret", "secret": "s"}
    assert await w.core.sweep_orphans(w.tenant, "fake") == [orphan]
