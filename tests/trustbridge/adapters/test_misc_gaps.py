"""The fake adapter's edges, the loader's protocol check, core settings and the resource core."""

from datetime import UTC
from typing import Any

import pytest

from tests.trustbridge.adapters.conftest import PULL, World, make_tenant
from trustbridge.adapters.fake import (
    FakeAdapter,
    FakePlatform,
    fake_capabilities,
    make_fake_adapter,
)
from trustbridge.adapters.interface import (
    AdapterConfig,
    AdapterError,
    ChangeState,
    CredentialScope,
    CredentialSlot,
    CredentialState,
    NoCiCredential,
    WebhookTarget,
)
from trustbridge.adapters.loader import AdapterLoadError, instantiate
from trustbridge.adapters.resources import ResourceCore
from trustbridge.adapters.store import CoreSettings, now_utc_default


def _adapter(**caps: Any) -> tuple[FakePlatform, FakeAdapter, CredentialScope]:  # noqa: ANN401
    platform = FakePlatform()
    tenant = make_tenant()
    adapter = make_fake_adapter(platform, fake_capabilities(oidc=False, **caps))(
        AdapterConfig(tenant)
    )
    scope = CredentialScope.build(
        tenant,
        repository_id=PULL.repository_id,
        repository_owner_id=PULL.repository_owner_id,
        environment=None,
        permission="pull",
    )
    return platform, adapter, scope


def _slot(scope: CredentialScope, *, delivered: bool = False) -> CredentialSlot:
    return CredentialSlot(1, f"slot-{scope.key}", {}, delivered)


async def test_the_fake_adapter_needs_a_current_slot_and_reports_manual_steps() -> None:
    """Issuing or rotating without a slot is a permanent error; a manual step is passed on."""
    platform, adapter, scope = _adapter()
    empty = CredentialState(current=None)
    with pytest.raises(AdapterError, match="no current slot"):
        await adapter.ensure_credential(scope, empty)
    with pytest.raises(AdapterError, match="no current slot"):
        await adapter.rotate_begin(scope, empty)
    platform.manual_action = "approve"
    outcome = await adapter.ensure_credential(scope, CredentialState(current=_slot(scope)))
    assert outcome.change.state is ChangeState.NEEDS_MANUAL_ACTION


async def test_a_delivered_secret_is_left_alone_and_rotation_finish_is_idempotent() -> None:
    """Re-ensuring a delivered secret changes nothing; finishing without a previous deletes none."""
    platform, adapter, scope = _adapter()
    state = CredentialState(current=_slot(scope))
    issued = await adapter.ensure_credential(scope, state)
    assert issued.credential is not None
    delivered = CredentialState(current=_slot(scope, delivered=True))
    again = await adapter.ensure_credential(scope, delivered)
    assert (again.change.state, again.credential) == (ChangeState.UNCHANGED, None)
    finished = await adapter.rotate_finish(scope, delivered)
    assert finished.change.state is ChangeState.UNCHANGED
    assert platform.credentials  # the current credential is untouched


async def test_the_fake_adapter_has_no_outputs_without_a_ci_credential_and_is_healthy() -> None:
    """`NoCiCredential` yields no GitHub outputs; the health check passes."""
    _, adapter, scope = _adapter()
    _, no_ci, _ = _adapter(ci_credential=False)
    outcome = await no_ci.ensure_credential(scope, CredentialState(current=_slot(scope)))
    assert isinstance(outcome.credential, NoCiCredential)
    assert list(adapter.github_outputs(scope, NoCiCredential("none"))) == []
    assert (await adapter.health_check()).ok is True


async def test_webhooks_and_resources_converge_through_the_resource_core(
    secret_world: World,
) -> None:
    """Webhook creation is idempotent; a timeout is a retryable adapter error."""
    w = secret_world
    fast = CoreSettings(lease_ttl_seconds=1, adapter_call_timeout_seconds=0.01)
    resources = ResourceCore(w.deps, fast)
    target = WebhookTarget(w.tenant, "https://tb.example/hook", ("push",))
    first = await resources.ensure_webhook(w.tenant, "fake", "main", target)
    second = await resources.ensure_webhook(w.tenant, "fake", "main", target)
    assert (first.change.state, second.change.state) == (ChangeState.CREATED, ChangeState.UNCHANGED)
    w.platform.hang.add("ensure_webhook")
    with pytest.raises(AdapterError, match="timed out") as err:
        await resources.ensure_webhook(w.tenant, "fake", "main", target)
    assert err.value.retryable is True


def test_an_object_that_is_not_an_adapter_is_refused_by_the_loader() -> None:
    """`instantiate` checks the protocol, not just the class attributes."""

    class NotAnAdapter:
        name = "bad"
        api_version = 1

        def __init__(self, config: AdapterConfig) -> None:
            del config

    with pytest.raises(AdapterLoadError, match="does not implement"):
        instantiate(NotAnAdapter, AdapterConfig(make_tenant()))  # type: ignore[arg-type]


def test_core_settings_validate_their_bounds_and_the_default_clock_is_utc() -> None:
    """Invariant 11 and a positive attempt budget; the clock is timezone-aware."""
    with pytest.raises(ValueError, match="at least 1"):
        CoreSettings(max_attempts=0)
    with pytest.raises(ValueError, match="shorter than"):
        CoreSettings(lease_ttl_seconds=10, adapter_call_timeout_seconds=10)
    assert now_utc_default().tzinfo is UTC
