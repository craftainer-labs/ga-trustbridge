"""Contract tests for the GHCR adapter (ADR 0029): every I2 operation, both grant scopes.

GHCR needs no API exchange (see the module README), so there is no recorded traffic to replay;
the tests drive the adapter directly (it has no HTTP client to inject).
"""

from dataclasses import replace
from uuid import UUID

import pytest

from trustbridge.adapters.ghcr import GhcrAdapter
from trustbridge.adapters.ghcr.adapter import image_of, namespace_of
from trustbridge.adapters.interface import (
    AdapterConfig,
    AdapterError,
    ChangeState,
    CredentialScope,
    CredentialSlot,
    CredentialState,
    GitHubHost,
    IssuedSecret,
    NoCiCredential,
    OidcTrust,
    Permission,
    PlatformAdapter,
    ProjectRef,
    ProjectSpec,
    RepoSpec,
    ResourceState,
    Tenant,
    WebhookTarget,
    credential_name,
    resource_name,
)
from trustbridge.adapters.loader import instantiate, validate_capabilities

TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")


def make_tenant(kind: str = "github.com") -> Tenant:
    """Build the fixed tenant on a github.com or GHES host (mixed-case org)."""
    ghes = kind == "ghes"
    host = GitHubHost(
        name=kind,
        kind="ghes" if ghes else "github.com",
        api_url="https://ghes.example/api/v3" if ghes else "https://api.github.com",
        oidc_issuer="https://ghes.example/_services/token"
        if ghes
        else "https://token.actions.githubusercontent.com",
        oidc_id_claims=True,
        registry_host="containers.ghes.example" if ghes else "ghcr.io",
    )
    return Tenant(id=TENANT_ID, github_host=host, org="Acme")


TENANT = make_tenant()
GHES = make_tenant("ghes")


def make_scope(
    permission: Permission = "pull",
    *,
    repo: int | None = 10,
    platform_repository_key: str | None = None,
) -> CredentialScope:
    """Build a credential scope for owner 5."""
    scope = CredentialScope.build(
        TENANT,
        repository_id=repo,
        repository_owner_id=5,
        environment=None,
        permission=permission,
    )
    return replace(scope, platform_repository_key=platform_repository_key)


SCOPE = make_scope()
SLOT = CredentialSlot(1, credential_name(TENANT_ID, SCOPE.key, 1))


def make_adapter() -> GhcrAdapter:
    """Build the adapter for the tenant."""
    return GhcrAdapter(AdapterConfig(TENANT))


# ---- plugin surface (A7) ---------------------------------------------------
def test_capabilities_are_valid_and_provision_no_ci_credential() -> None:
    """GHCR: no OIDC, no CI credential, no rotation, no webhook, no privileged credential."""
    caps = make_adapter().capabilities()
    validate_capabilities(caps)
    assert not caps.oidc
    assert caps.oidc_hosts == frozenset()
    assert not caps.ci_credential
    assert caps.rotation == "none"
    assert caps.webhook is None
    assert not caps.needs_privileged_credential
    assert caps.github_hosts == frozenset({"github.com", "ghes", "ghe.com"})


def test_the_adapter_implements_the_protocol_and_the_loader_accepts_it() -> None:
    """The loader instantiates it with the config only."""
    assert isinstance(make_adapter(), PlatformAdapter)
    assert GhcrAdapter.name == "ghcr"
    assert isinstance(instantiate(GhcrAdapter, AdapterConfig(TENANT)), GhcrAdapter)


async def test_health_needs_no_connection() -> None:
    """There is no connection to lose."""
    assert (await make_adapter().health_check()).ok


# ---- projects --------------------------------------------------------------
async def test_ensure_project_records_the_lowercased_org_as_namespace() -> None:
    """The namespace is the org lowercased; the registry host is the host's."""
    applied = await make_adapter().ensure_project(TENANT, ProjectSpec("default"), None)
    assert applied.change.state is ChangeState.CREATED
    assert applied.state is not None
    assert applied.state.name == resource_name(TENANT_ID, "project", "default")
    assert applied.state.ref == {
        "kind": "namespace",
        "namespace": "acme",
        "registry_host": "ghcr.io",
    }
    assert namespace_of(TENANT) == "acme"


async def test_ensure_project_is_idempotent_and_follows_a_host_change() -> None:
    """Same ref is UNCHANGED, a different registry host is UPDATED, an empty ref is CREATED."""
    adapter = make_adapter()
    first = await adapter.ensure_project(TENANT, ProjectSpec("default"), None)
    assert first.state is not None
    again = await adapter.ensure_project(TENANT, ProjectSpec("default"), first.state)
    assert again.change.state is ChangeState.UNCHANGED
    assert again.state == first.state
    moved = await adapter.ensure_project(GHES, ProjectSpec("default"), first.state)
    assert moved.change.state is ChangeState.UPDATED
    assert moved.state is not None
    assert moved.state.name == first.state.name
    assert moved.state.ref["registry_host"] == "containers.ghes.example"
    empty = ResourceState(name=first.state.name, ref={})
    assert (await adapter.ensure_project(TENANT, ProjectSpec("default"), empty)).change.state is (
        ChangeState.CREATED
    )


# ---- repositories ----------------------------------------------------------
async def project_ref(tenant: Tenant = TENANT) -> ProjectRef:
    """Return the project the core would pass on."""
    applied = await make_adapter().ensure_project(tenant, ProjectSpec("default"), None)
    assert applied.state is not None
    return ProjectRef(tenant, applied.state)


async def test_ensure_repository_without_a_github_repo_just_records_the_package() -> None:
    """No repository to grant: nothing manual, only a record (namespace-wide use)."""
    p = await project_ref()
    applied = await make_adapter().ensure_repository(p, RepoSpec("web"), None)
    assert applied.change.state is ChangeState.CREATED
    assert applied.change.manual_action is None
    assert applied.state is not None
    assert applied.state.ref["image"] == "ghcr.io/acme/web"
    assert applied.state.ref["package"] == "web"
    again = await make_adapter().ensure_repository(p, RepoSpec("web"), applied.state)
    assert again.change.state is ChangeState.UNCHANGED
    assert again.state == applied.state


async def test_ensure_repository_with_a_github_repo_needs_the_ui_grant() -> None:
    """Repository access has no REST endpoint, so it is NEEDS_MANUAL_ACTION with instructions."""
    p = await project_ref()
    applied = await make_adapter().ensure_repository(
        p, RepoSpec("web", package_type="container", repository_id=10), None
    )
    assert applied.change.state is ChangeState.NEEDS_MANUAL_ACTION
    action = applied.change.manual_action
    assert action is not None
    assert "ghcr.io/acme/web" in action
    assert "Manage Actions access" in action
    assert "10" in action
    assert applied.state is not None
    assert applied.state.name == resource_name(TENANT_ID, "repository", "ghcr.io/acme/web")


async def test_ensure_repository_uses_the_ghes_registry_host() -> None:
    """The image path is derived from the host's registry host."""
    p = await project_ref(GHES)
    applied = await make_adapter().ensure_repository(p, RepoSpec("web"), None)
    assert applied.state is not None
    assert applied.state.ref["image"] == "containers.ghes.example/acme/web"
    assert image_of("ghcr.io", "acme", "x") == "ghcr.io/acme/x"


async def test_ensure_repository_falls_back_to_the_tenant_when_the_project_ref_is_empty() -> None:
    """A project state without a ref still resolves namespace and host from the tenant."""
    bare = ProjectRef(TENANT, ResourceState(name="p", ref={}))
    applied = await make_adapter().ensure_repository(bare, RepoSpec("web"), None)
    assert applied.state is not None
    assert applied.state.ref["image"] == "ghcr.io/acme/web"


async def test_ensure_repository_keeps_the_prior_name() -> None:
    """A stored name is never renamed."""
    p = await project_ref()
    prior = ResourceState(name="stored-name", ref={})
    applied = await make_adapter().ensure_repository(p, RepoSpec("web"), prior)
    assert applied.state is not None
    assert applied.state.name == "stored-name"
    assert applied.change.state is ChangeState.CREATED


@pytest.mark.parametrize("key", ["Web", "a/b", "", "-web", "web-", "we b", "w" * 129])
async def test_ensure_repository_rejects_invalid_package_names(key: str) -> None:
    """Names that are not a single lowercase image segment are non-retryable errors."""
    p = await project_ref()
    with pytest.raises(AdapterError) as err:
        await make_adapter().ensure_repository(p, RepoSpec(key), None)
    assert not err.value.retryable


async def test_ensure_repository_rejects_other_package_types() -> None:
    """Only container packages are supported."""
    p = await project_ref()
    with pytest.raises(AdapterError) as err:
        await make_adapter().ensure_repository(p, RepoSpec("web", package_type="maven"), None)
    assert not err.value.retryable


# ---- webhooks and delete ---------------------------------------------------
async def test_ensure_webhook_is_refused() -> None:
    """GHCR has no webhooks of its own."""
    with pytest.raises(AdapterError) as err:
        await make_adapter().ensure_webhook(WebhookTarget(TENANT, "https://tb.example/h"), None)
    assert not err.value.retryable


async def test_delete_never_removes_a_package() -> None:
    """Delete forgets the record only; the result is UNCHANGED with no state."""
    p = await project_ref()
    applied = await make_adapter().ensure_repository(p, RepoSpec("web"), None)
    assert applied.state is not None
    deleted = await make_adapter().delete(applied.state)
    assert deleted.change.state is ChangeState.UNCHANGED
    assert deleted.state is None
    assert deleted.change.detail is not None


# ---- credentials (both grant scopes) -----------------------------------------
async def test_project_wide_scope_returns_no_ci_credential() -> None:
    """With no `platform_repository_key` the grant is namespace-wide (GITHUB_TOKEN)."""
    adapter = make_adapter()
    outcome = await adapter.ensure_credential(SCOPE, CredentialState(current=SLOT))
    assert outcome.change.state is ChangeState.CREATED
    assert isinstance(outcome.credential, NoCiCredential)
    assert "GITHUB_TOKEN" in outcome.credential.reason
    assert "only" not in outcome.credential.reason
    assert outcome.state.current is not None
    assert outcome.state.current.delivered


async def test_per_repository_scope_names_the_package() -> None:
    """With `platform_repository_key` set the reason names that package's own grant."""
    scope = make_scope(platform_repository_key="web")
    outcome = await make_adapter().ensure_credential(scope, CredentialState(current=SLOT))
    assert isinstance(outcome.credential, NoCiCredential)
    assert "'web'" in outcome.credential.reason
    assert "only" in outcome.credential.reason


async def test_ensure_credential_is_unchanged_once_delivered() -> None:
    """A re-run on a delivered slot is UNCHANGED and keeps `previous`."""
    prev = CredentialSlot(0, "old", delivered=True)
    prior = CredentialState(current=replace(SLOT, delivered=True), previous=prev)
    outcome = await make_adapter().ensure_credential(SCOPE, prior)
    assert outcome.change.state is ChangeState.UNCHANGED
    assert outcome.state == prior
    assert isinstance(outcome.credential, NoCiCredential)


async def test_credential_calls_need_a_slot() -> None:
    """No current slot is a non-retryable error."""
    with pytest.raises(AdapterError) as err:
        await make_adapter().ensure_credential(SCOPE, CredentialState(current=None))
    assert not err.value.retryable


async def test_rotation_has_nothing_to_rotate() -> None:
    """`rotate_begin` returns the same no-CI answer; `rotate_finish` changes nothing."""
    adapter = make_adapter()
    prior = CredentialState(current=SLOT)
    begin = await adapter.rotate_begin(SCOPE, prior)
    assert isinstance(begin.credential, NoCiCredential)
    finish = await adapter.rotate_finish(SCOPE, prior)
    assert finish.change.state is ChangeState.UNCHANGED
    assert finish.credential is None
    assert finish.state.current == SLOT


async def test_delete_credential_and_listing_are_empty() -> None:
    """Nothing exists on GHCR for a credential, so there is nothing to delete or to sweep."""
    adapter = make_adapter()
    outcome = await adapter.delete_credential(SCOPE, CredentialState(current=SLOT))
    assert outcome.change.state is ChangeState.UNCHANGED
    assert outcome.state.current is None
    assert outcome.credential is None
    assert await adapter.list_credentials(TENANT) == []


def test_github_outputs_are_empty_and_pure() -> None:
    """No secret or variable is written for any credential kind (A1, A2)."""
    adapter = make_adapter()
    for scope in (SCOPE, make_scope(platform_repository_key="web")):
        assert adapter.github_outputs(scope, NoCiCredential("x")) == []
    assert adapter.github_outputs(SCOPE, OidcTrust("i", None, {})) == []
    assert adapter.github_outputs(SCOPE, IssuedSecret({})) == []
