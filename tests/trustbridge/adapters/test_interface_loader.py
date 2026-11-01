"""The adapter interface helpers, the entry-point loader and the fake adapter's contract."""

from dataclasses import replace
from importlib.metadata import EntryPoint
from uuid import uuid4

import pytest
from pydantic import SecretStr

from tests.trustbridge.adapters.conftest import PUSH_PROD, make_tenant
from trustbridge.adapters.fake import (
    FakeAdapter,
    FakePlatform,
    fake_capabilities,
    make_fake_adapter,
)
from trustbridge.adapters.interface import (
    API_VERSION,
    AdapterConfig,
    CredentialScope,
    CredentialSlot,
    CredentialState,
    IssuedSecret,
    OidcTrust,
    PlatformAdapter,
    credential_name,
    resource_name,
    scope_key,
)
from trustbridge.adapters.loader import (
    AdapterLoadError,
    check_api_version,
    instantiate,
    load_adapter_classes,
    validate_capabilities,
)


def test_scope_key_is_deterministic_and_distinguishes_scopes() -> None:
    """The key hashes (repository|org, environment|"", permission)."""
    assert scope_key(1, None, "pull") == scope_key(1, None, "pull")
    assert (
        len(
            {
                scope_key(1, None, "pull"),
                scope_key(None, None, "pull"),
                scope_key(1, "prod", "pull"),
                scope_key(1, None, "push"),
            }
        )
        == 4
    )


def test_credential_names_carry_the_full_tenant_id_and_generation() -> None:
    """Invariants A4 and 0028-24."""
    tenant = uuid4()
    name = credential_name(tenant, "ab" * 16, 3)
    assert name == f"tb-{tenant.hex}-{'ab' * 8}-g3"
    assert resource_name(tenant, "project", "p").startswith(f"tb-{tenant.hex}-")


def test_adapter_config_repr_never_shows_secret_values() -> None:
    """A5: connection data stays out of logs."""
    cfg = AdapterConfig(make_tenant(), {"token": SecretStr("hunter2")}, SecretStr("pat-secret"))
    assert "hunter2" not in repr(cfg)
    assert "pat-secret" not in repr(cfg)


def test_capability_validation_a7() -> None:
    """A7: oidc_hosts is empty without OIDC and a subset of github_hosts."""
    good = fake_capabilities(oidc=True)
    validate_capabilities(good)
    with pytest.raises(AdapterLoadError, match="empty"):
        validate_capabilities(replace(fake_capabilities(oidc=False), oidc_hosts=good.oidc_hosts))
    with pytest.raises(AdapterLoadError, match="subset"):
        validate_capabilities(replace(good, github_hosts=frozenset({"github.com"})))
    with pytest.raises(AdapterLoadError, match="positive"):
        validate_capabilities(replace(good, credential_limit=0))


def test_api_version_range() -> None:
    """The supported range is MIN..API_VERSION: newer adapters are refused."""
    check_api_version(API_VERSION)
    with pytest.raises(AdapterLoadError):
        check_api_version(API_VERSION + 1)
    with pytest.raises(AdapterLoadError):
        check_api_version(0)


def _ep(name: str, target: str) -> EntryPoint:
    return EntryPoint(name=name, value=target, group="trustbridge.adapters")


def test_only_enabled_adapters_are_loaded() -> None:
    """An adapter not in ADAPTERS_ENABLED is never imported."""
    eps = [
        _ep("fake", "trustbridge.adapters.fake:FakeAdapter"),
        _ep("broken", "trustbridge.adapters.does_not_exist:Nope"),
    ]
    assert load_adapter_classes(["fake"], eps=eps) == {"fake": FakeAdapter}


@pytest.mark.parametrize(
    ("enabled", "eps", "needle"),
    [
        (["missing"], [], "no entry point"),
        (
            ["broken"],
            [_ep("broken", "trustbridge.adapters.does_not_exist:Nope")],
            "failed to import",
        ),
        (["other"], [_ep("other", "trustbridge.adapters.fake:FakeAdapter")], "differs"),
    ],
)
def test_load_errors(enabled: list[str], eps: list[EntryPoint], needle: str) -> None:
    """Missing, unimportable and misnamed adapters are rejected at load."""
    with pytest.raises(AdapterLoadError, match=needle):
        load_adapter_classes(enabled, eps=eps)


def test_instantiate_validates_capabilities() -> None:
    """The loader instantiates with `cls(config)` and validates what `capabilities()` says."""
    platform = FakePlatform()
    bad = replace(fake_capabilities(oidc=False), oidc_hosts=frozenset({"ghes"}))
    cls = make_fake_adapter(platform, bad)
    with pytest.raises(AdapterLoadError):
        instantiate(cls, AdapterConfig(make_tenant()))
    ok = instantiate(make_fake_adapter(platform), AdapterConfig(make_tenant()))
    assert isinstance(ok, PlatformAdapter)


# ---- the fake adapter honours the contract NG5 will test ---------------------------------
def _scope(*, eligible: bool) -> tuple[CredentialScope, CredentialState]:
    tenant = make_tenant()
    scope = CredentialScope.build(
        tenant,
        repository_id=PUSH_PROD.repository_id,
        repository_owner_id=PUSH_PROD.repository_owner_id,
        environment="prod",
        permission="push",
    )
    slot = CredentialSlot(1, credential_name(tenant.id, scope.key, 1))
    return replace(scope, oidc_eligible=eligible), CredentialState(current=slot)


async def test_fake_secret_issuance_is_idempotent_by_name() -> None:
    """A2 and A3: a repeat with the same prior replaces the value under the same name."""
    platform = FakePlatform()
    adapter = make_fake_adapter(platform)(AdapterConfig(make_tenant()))
    scope, prior = _scope(eligible=False)
    first = await adapter.ensure_credential(scope, prior)
    second = await adapter.ensure_credential(scope, prior)
    assert len(platform.credentials) == 1
    assert isinstance(first.credential, IssuedSecret)
    assert second.change.state.value == "updated"
    # With `ref` emptied the adapter still finds the object by name.
    assert prior.current is not None
    assert not prior.current.ref


async def test_fake_trust_is_returned_on_every_outcome_with_exact_claims() -> None:
    """A1 and A6: trust comes back also when UNCHANGED, claims are exactly the allowed set."""
    platform = FakePlatform()
    adapter = make_fake_adapter(platform, fake_capabilities(oidc=True))(
        AdapterConfig(make_tenant())
    )
    scope, prior = _scope(eligible=True)
    first = await adapter.ensure_credential(scope, prior)
    again = await adapter.ensure_credential(scope, first.state)
    assert again.change.state.value == "unchanged"
    assert isinstance(again.credential, OidcTrust)
    assert set(again.credential.claims) == {"repository_owner_id", "repository_id", "environment"}
    assert adapter.github_outputs(scope, again.credential) == adapter.github_outputs(
        scope, again.credential
    )
