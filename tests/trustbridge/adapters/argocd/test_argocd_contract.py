"""Contract tests for the Argo CD adapter: it replays recorded-style API exchanges (no network).

Each test names the ADR 0029 invariant it covers. The fixtures under `fixtures/` are built from
the Argo CD declarative-setup documentation and the Kubernetes API conventions; the module README
lists what is unconfirmed.
"""

from dataclasses import replace
from uuid import UUID

import pytest
from pydantic import SecretStr

from tests.trustbridge.adapters.argocd.replay import (
    BASE_URL,
    SA_TOKEN,
    Replay,
    load_scenario,
)
from trustbridge.adapters.argocd import ArgoCDAdapter
from trustbridge.adapters.argocd.adapter import INSTALL_APP_ACTION, web_base
from trustbridge.adapters.interface import (
    AdapterConfig,
    AdapterConfigurationError,
    AdapterError,
    ChangeState,
    CredentialScope,
    CredentialSlot,
    CredentialState,
    GitHubHost,
    IssuedSecret,
    NoCiCredential,
    Permission,
    PlatformAdapter,
    ProjectRef,
    ProjectSpec,
    RepoSpec,
    ResourceState,
    Tenant,
    WebhookAuth,
    WebhookTarget,
    credential_name,
    resource_name,
)
from trustbridge.adapters.loader import instantiate, validate_capabilities

TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")
HOOK_URL = "https://tb.example/hooks/argocd"
HOOK_SECRET = "whsec-dummy-value"  # noqa: S105 - a dummy used only in these fixtures
APP_KEY = "dummy-app-key-material"
REPO_URL = "https://github.com/acme/images"


def make_tenant(kind: str = "github.com", org: str = "acme") -> Tenant:
    """Build the fixed tenant on a github.com or GHES host."""
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
    return Tenant(id=TENANT_ID, github_host=host, org=org)


TENANT = make_tenant()


def make_scope(
    permission: Permission,
    *,
    repo: int | None = 10,
    key: str | None = None,
    tenant: Tenant = TENANT,
) -> CredentialScope:
    """Build a credential scope for owner 5; `key` is the platform repository it is limited to."""
    scope = CredentialScope.build(
        tenant,
        repository_id=repo,
        repository_owner_id=5,
        environment=None,
        permission=permission,
    )
    return replace(scope, platform_repository_key=key)


REPO_SCOPE = make_scope("pull", key="images")
ORG_SCOPE = make_scope("pull", repo=None)
PROJECT = resource_name(TENANT_ID, "project", "default")
CONTEXT = {
    "project": PROJECT,
    "hook": resource_name(TENANT_ID, "webhook", HOOK_URL),
    "cred_pull": credential_name(TENANT_ID, REPO_SCOPE.key, 1),
    "cred_org": credential_name(TENANT_ID, ORG_SCOPE.key, 1),
    "prev": credential_name(TENANT_ID, REPO_SCOPE.key, 0),
}
REPO_SLOT = CredentialSlot(1, CONTEXT["cred_pull"])
ORG_SLOT = CredentialSlot(1, CONTEXT["cred_org"])
PREV_SLOT = CredentialSlot(0, CONTEXT["prev"], {"kind": "secret", "secret": CONTEXT["prev"]}, True)


def config(
    tenant: Tenant = TENANT, overrides: dict[str, SecretStr | None] | None = None
) -> AdapterConfig:
    """Build the tenant's connection data (the keys the manifest flow stores, plus the URL)."""
    connection = {
        "base_url": SecretStr(BASE_URL),
        "hook-secret": SecretStr(HOOK_SECRET),
        "app_id": SecretStr("123456"),
        "private_key": SecretStr(APP_KEY),
        "installation_id": SecretStr("7890"),
    }
    for key, value in (overrides or {}).items():
        if value is None:
            connection.pop(key, None)
        else:
            connection[key] = value
    return AdapterConfig(tenant, connection, SecretStr(SA_TOKEN))


def build(
    file: str,
    scenario: str,
    tenant: Tenant = TENANT,
    overrides: dict[str, SecretStr | None] | None = None,
) -> tuple[Replay, ArgoCDAdapter]:
    """Return a replay of the scenario and an adapter wired to it."""
    replay = Replay(load_scenario(file, scenario, CONTEXT))
    return replay, ArgoCDAdapter(config(tenant, overrides), transport=replay.transport())


def offline_adapter(**overrides: SecretStr | None) -> ArgoCDAdapter:
    """An adapter whose transport must never be used."""
    return ArgoCDAdapter(config(overrides=overrides), transport=Replay([]).transport())


def prior_for(slot: CredentialSlot, previous: CredentialSlot | None = None) -> CredentialState:
    """The write-ahead state the core commits before a call."""
    return CredentialState(current=slot, previous=previous)


# ---- capabilities and loading (A7) -----------------------------------------------------------
def test_capabilities_match_the_adr_matrix() -> None:
    """ADR 0029 matrix: no OIDC, no CI credential, no rotation, a static-header webhook."""
    adapter = offline_adapter()
    caps = adapter.capabilities()
    validate_capabilities(caps)
    assert not caps.oidc
    assert caps.oidc_hosts == frozenset()
    assert not caps.ci_credential
    assert caps.rotation == "none"
    assert caps.needs_privileged_credential
    assert caps.github_hosts == frozenset({"github.com", "ghes", "ghe.com"})
    assert caps.webhook is not None
    assert caps.webhook.auth is WebhookAuth.STATIC_HEADER
    assert caps.webhook.header == "Authorization"
    assert caps.webhook.trigger_only
    assert isinstance(adapter, PlatformAdapter)


def test_the_loader_instantiates_with_the_config_only() -> None:
    """The loader calls `cls(config)`."""
    assert isinstance(instantiate(ArgoCDAdapter, config()), ArgoCDAdapter)


@pytest.mark.parametrize(
    ("kind", "api_url", "web"),
    [
        ("github.com", "https://api.github.com", "https://github.com"),
        ("ghes", "https://ghes.example/api/v3/", "https://ghes.example"),
        ("ghe.com", "https://api.acme.ghe.com", "https://acme.ghe.com"),
    ],
)
def test_the_web_url_derives_from_the_api_url(kind: str, api_url: str, web: str) -> None:
    """Repository URLs use the host's web address, derived from `GitHubHost.api_url`."""
    host = replace(TENANT.github_host, kind=kind, api_url=api_url)  # type: ignore[arg-type]
    assert web_base(host) == web


# ---- configuration errors --------------------------------------------------------------------
async def test_missing_service_account_token_is_a_configuration_error() -> None:
    """A13: no privileged credential means non-retryable configuration error and no request."""
    adapter = ArgoCDAdapter(
        AdapterConfig(TENANT, {"base_url": SecretStr(BASE_URL)}, None),
        transport=Replay([]).transport(),
    )
    with pytest.raises(AdapterConfigurationError):
        await adapter.ensure_project(TENANT, ProjectSpec("default"), None)
    health = await adapter.health_check()
    assert not health.ok
    assert health.detail is not None


@pytest.mark.parametrize("url", [None, "http://kube.example", "https://kube.example?x=1"])
async def test_the_base_url_must_be_an_https_url(url: str | None) -> None:
    """A missing, plain-http or query-carrying base URL is refused before any request."""
    adapter = offline_adapter(base_url=SecretStr(url) if url else None)
    with pytest.raises(AdapterConfigurationError):
        await adapter.ensure_project(TENANT, ProjectSpec("default"), None)


async def test_the_namespace_can_be_set_in_connection_data() -> None:
    """Objects live in `argocd` unless the connection data names another namespace."""
    replay, adapter = build(
        "credentials.json", "health_ok", overrides={"namespace": SecretStr("gitops")}
    )
    for exchange in replay.exchanges:
        exchange["request"]["path"] = exchange["request"]["path"].replace("/argocd/", "/gitops/")
    assert (await adapter.health_check()).ok
    replay.assert_exhausted()


# ---- projects (A2, A3) -----------------------------------------------------------------------
async def test_ensure_project_creates_then_repeats_unchanged() -> None:
    """A2: the first call creates, the repeat (same prior, then a ref-less prior) is UNCHANGED."""
    spec = ProjectSpec("default", "images")
    replay, adapter = build("projects.json", "create")
    created = await adapter.ensure_project(TENANT, spec, None)
    replay.assert_exhausted()
    assert created.change.state is ChangeState.CREATED
    assert created.state == ResourceState(PROJECT, {"kind": "project", "name": PROJECT})
    for prior in (created.state, ResourceState(name=PROJECT)):
        replay, adapter = build("projects.json", "exists")
        again = await adapter.ensure_project(TENANT, spec, prior)
        replay.assert_exhausted()
        assert again.change.state is ChangeState.UNCHANGED
        assert again.state == created.state
        assert [r.method for r in replay.seen] == ["GET"]


async def test_ensure_project_updates_a_drifted_description() -> None:
    """Reconcile: a stale description is PUT back, keeping the other fields and resourceVersion."""
    replay, adapter = build("projects.json", "drift")
    applied = await adapter.ensure_project(TENANT, ProjectSpec("default", "images"), None)
    replay.assert_exhausted()
    assert applied.change.state is ChangeState.UPDATED


async def test_a_project_creation_race_is_retried_not_adopted() -> None:
    """A 409 on POST is retryable: the retry re-reads it and checks that it is ours."""
    replay, adapter = build("projects.json", "race")
    with pytest.raises(AdapterError) as err:
        await adapter.ensure_project(TENANT, ProjectSpec("default", "images"), None)
    replay.assert_exhausted()
    assert err.value.retryable


async def test_ensure_project_refuses_an_unmanaged_project() -> None:
    """A project of the generated name that trustbridge did not create is never adopted."""
    replay, adapter = build("projects.json", "unmarked")
    with pytest.raises(AdapterError) as err:
        await adapter.ensure_project(TENANT, ProjectSpec("default", "images"), None)
    replay.assert_exhausted()
    assert not err.value.retryable
    assert [r.method for r in replay.seen] == ["GET"]


async def test_a_concurrent_project_change_is_retryable() -> None:
    """A 409 on PUT (stale resourceVersion) is retried; the retry re-reads the project."""
    replay, adapter = build("projects.json", "drift_conflict")
    with pytest.raises(AdapterError) as err:
        await adapter.ensure_project(TENANT, ProjectSpec("default", "images"), None)
    replay.assert_exhausted()
    assert err.value.retryable


# ---- repositories ----------------------------------------------------------------------------
PROJECT_REF = ProjectRef(TENANT, ResourceState(PROJECT, {"kind": "project", "name": PROJECT}))
REPO_STATE = ResourceState(REPO_URL, {"kind": "repository", "project": PROJECT, "url": REPO_URL})


async def test_ensure_repository_allows_the_git_url_in_the_project() -> None:
    """The repository's Git URL joins `sourceRepos`; the state names the URL and the project."""
    replay, adapter = build("repositories.json", "add")
    applied = await adapter.ensure_repository(PROJECT_REF, RepoSpec("images"), None)
    replay.assert_exhausted()
    assert applied.change.state is ChangeState.UPDATED
    assert applied.state == REPO_STATE


async def test_ensure_repository_is_idempotent() -> None:
    """A2/A3: a URL that is already allowed is UNCHANGED and nothing is written."""
    replay, adapter = build("repositories.json", "exists")
    applied = await adapter.ensure_repository(PROJECT_REF, RepoSpec("images"), REPO_STATE)
    replay.assert_exhausted()
    assert applied.change.state is ChangeState.UNCHANGED
    assert [r.method for r in replay.seen] == ["GET"]


async def test_ensure_repository_needs_the_project() -> None:
    """A missing AppProject is a non-retryable error, not a silent create."""
    _, adapter = build("repositories.json", "project_missing")
    with pytest.raises(AdapterError) as err:
        await adapter.ensure_repository(PROJECT_REF, RepoSpec("images"), None)
    assert not err.value.retryable


async def test_ensure_repository_refuses_an_unmanaged_project() -> None:
    """The adapter does not edit an AppProject it did not create."""
    replay, adapter = build("repositories.json", "unmarked")
    with pytest.raises(AdapterError) as err:
        await adapter.ensure_repository(PROJECT_REF, RepoSpec("images"), None)
    replay.assert_exhausted()
    assert not err.value.retryable
    assert [r.method for r in replay.seen] == ["GET"]


async def test_ensure_repository_conflict_is_retryable() -> None:
    """A stale resourceVersion on the project PUT is retried."""
    _, adapter = build("repositories.json", "conflict")
    with pytest.raises(AdapterError) as err:
        await adapter.ensure_repository(PROJECT_REF, RepoSpec("images"), None)
    assert err.value.retryable


@pytest.mark.parametrize("key", ["a/b", "..", ".", "", "x" * 101])
async def test_ensure_repository_rejects_unusable_keys(key: str) -> None:
    """A key that is not a GitHub repository name is a configuration error, before any call."""
    with pytest.raises(AdapterConfigurationError):
        await offline_adapter().ensure_repository(PROJECT_REF, RepoSpec(key), None)


async def test_an_unusable_organization_is_a_configuration_error() -> None:
    """The org part of every URL is validated like the repository key."""
    tenant = make_tenant(org="a/b")
    with pytest.raises(AdapterConfigurationError):
        await offline_adapter().ensure_repository(
            ProjectRef(tenant, PROJECT_REF.project), RepoSpec("images"), None
        )


# ---- webhooks (STATIC_HEADER bearer) ---------------------------------------------------------
TARGET = WebhookTarget(TENANT, HOOK_URL, (), "hook-secret")
HOOK_STATE = ResourceState(CONTEXT["hook"], {"kind": "webhook", "name": CONTEXT["hook"]})


async def test_ensure_webhook_writes_the_service_and_the_bearer_token() -> None:
    """The token goes to the notifications Secret first, then the service references it."""
    replay, adapter = build("webhooks.json", "create")
    applied = await adapter.ensure_webhook(TARGET, None)
    replay.assert_exhausted()
    assert applied.change.state is ChangeState.CREATED
    assert applied.state == HOOK_STATE
    assert [r.method for r in replay.seen] == ["GET", "GET", "PATCH", "PATCH"]
    assert HOOK_SECRET not in repr(applied.change)


async def test_ensure_webhook_is_idempotent() -> None:
    """A2/A3: a service and token that already match are UNCHANGED (also with an empty ref)."""
    replay, adapter = build("webhooks.json", "exists")
    applied = await adapter.ensure_webhook(TARGET, ResourceState(name=CONTEXT["hook"]))
    replay.assert_exhausted()
    assert applied.change.state is ChangeState.UNCHANGED
    assert [r.method for r in replay.seen] == ["GET", "GET"]


async def test_ensure_webhook_reconciles_a_changed_url_and_token() -> None:
    """Reconcile: a drifted URL or a rotated token is patched back and reported UPDATED."""
    replay, adapter = build("webhooks.json", "drift")
    applied = await adapter.ensure_webhook(TARGET, None)
    replay.assert_exhausted()
    assert applied.change.state is ChangeState.UPDATED


@pytest.mark.parametrize("scenario", ["missing_cm", "missing_secret"])
async def test_ensure_webhook_needs_the_notifications_objects(scenario: str) -> None:
    """Without Argo CD notifications installed nothing can be written; retrying cannot help."""
    _, adapter = build("webhooks.json", scenario)
    with pytest.raises(AdapterError) as err:
        await adapter.ensure_webhook(TARGET, None)
    assert not err.value.retryable


async def test_ensure_webhook_needs_a_secret() -> None:
    """An unauthenticated webhook is refused; a secret_ref without a value is a config error."""
    with pytest.raises(AdapterConfigurationError):
        await offline_adapter().ensure_webhook(replace(TARGET, secret_ref=None), None)
    with pytest.raises(AdapterConfigurationError):
        await offline_adapter(**{"hook-secret": None}).ensure_webhook(TARGET, None)


# ---- deleting resources ----------------------------------------------------------------------
DELETE_CASES = [
    (
        "projects.json",
        ResourceState(PROJECT, {"kind": "project", "name": PROJECT}),
        "delete_absent",
    ),
    ("repositories.json", REPO_STATE, "delete_absent"),
    ("repositories.json", REPO_STATE, "delete_not_listed"),
    ("webhooks.json", HOOK_STATE, "delete_absent"),
]


@pytest.mark.parametrize(("file", "prior", "absent"), DELETE_CASES)
async def test_delete_removes_then_reports_absent_as_unchanged(
    file: str, prior: ResourceState, absent: str
) -> None:
    """A2: delete is idempotent; an absent object (or entry) is UNCHANGED."""
    if absent != "delete_not_listed":
        replay, adapter = build(file, "delete")
        deleted = await adapter.delete(prior)
        replay.assert_exhausted()
        assert deleted.change.state is ChangeState.DELETED
        assert deleted.state is None
    replay, adapter = build(file, absent)
    again = await adapter.delete(prior)
    replay.assert_exhausted()
    assert again.change.state is ChangeState.UNCHANGED


@pytest.mark.parametrize(
    ("file", "prior"),
    [
        ("projects.json", ResourceState(PROJECT, {"kind": "project", "name": PROJECT})),
        ("repositories.json", REPO_STATE),
    ],
)
async def test_delete_leaves_an_unmanaged_object_alone(file: str, prior: ResourceState) -> None:
    """An object without the managed-by label is not ours: nothing is deleted, UNCHANGED."""
    replay, adapter = build(file, "unmarked")
    outcome = await adapter.delete(prior)
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.UNCHANGED
    assert [r.method for r in replay.seen] == ["GET"]


async def test_delete_rejects_an_unknown_resource_kind() -> None:
    """A state without a known kind is a non-retryable error and sends nothing."""
    with pytest.raises(AdapterError) as err:
        await offline_adapter().delete(ResourceState("x"))
    assert not err.value.retryable


# ---- credentials: per-repository and org-wide grants (I2 credential scope) -------------------
async def test_a_repository_scope_writes_a_repository_secret_for_exactly_that_repository() -> None:
    """`platform_repository_key` set: a `repository` Secret whose URL is that one repository."""
    replay, adapter = build("credentials.json", "repo_create")
    outcome = await adapter.ensure_credential(REPO_SCOPE, prior_for(REPO_SLOT, PREV_SLOT))
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.CREATED
    assert isinstance(outcome.credential, NoCiCredential)
    assert outcome.state.current == replace(
        REPO_SLOT, ref={"kind": "secret", "secret": REPO_SLOT.name}, delivered=True
    )
    assert outcome.state.previous == PREV_SLOT


async def test_an_org_scope_writes_a_repo_creds_template_for_the_org() -> None:
    """Unset: a `repo-creds` Secret whose URL prefix is the org, with a trailing slash."""
    replay, adapter = build("credentials.json", "org_create")
    outcome = await adapter.ensure_credential(ORG_SCOPE, prior_for(ORG_SLOT))
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.CREATED
    assert isinstance(outcome.credential, NoCiCredential)


async def test_a_ghes_host_adds_the_enterprise_base_url() -> None:
    """GHES: the Secret carries `githubAppEnterpriseBaseUrl` and the host's own URLs."""
    ghes = make_tenant("ghes")
    replay, adapter = build("credentials.json", "org_create_ghes", ghes)
    outcome = await adapter.ensure_credential(
        make_scope("pull", repo=None, tenant=ghes), prior_for(ORG_SLOT)
    )
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.CREATED


async def test_a_delivered_credential_is_unchanged_and_ref_emptied_state_is_rebuilt() -> None:
    """A1/A2: the same Secret is UNCHANGED and still reports no CI credential."""
    replay, adapter = build("credentials.json", "unchanged")
    outcome = await adapter.ensure_credential(REPO_SCOPE, prior_for(REPO_SLOT))
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.UNCHANGED
    assert isinstance(outcome.credential, NoCiCredential)
    assert outcome.state.current is not None
    assert outcome.state.current.delivered


async def test_a_scope_that_gains_a_repository_grant_is_rewritten_in_place() -> None:
    """Reconcile: an org template Secret under the same name becomes a repository Secret."""
    replay, adapter = build("credentials.json", "drift_scope")
    outcome = await adapter.ensure_credential(REPO_SCOPE, prior_for(REPO_SLOT))
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.UPDATED


async def test_credential_creation_race_is_retried_not_adopted() -> None:
    """A 409 on POST is retryable: the retry re-reads the Secret and checks that it is ours."""
    replay, adapter = build("credentials.json", "race")
    with pytest.raises(AdapterError) as err:
        await adapter.ensure_credential(REPO_SCOPE, prior_for(REPO_SLOT))
    replay.assert_exhausted()
    assert err.value.retryable


async def test_an_unmanaged_secret_is_neither_adopted_nor_deleted() -> None:
    """A Secret of the generated name without the managed-by label is refused and left alone."""
    replay, adapter = build("credentials.json", "unmarked")
    with pytest.raises(AdapterError) as err:
        await adapter.ensure_credential(REPO_SCOPE, prior_for(REPO_SLOT))
    replay.assert_exhausted()
    assert not err.value.retryable
    replay, adapter = build("credentials.json", "unmarked")
    outcome = await adapter.delete_credential(REPO_SCOPE, prior_for(REPO_SLOT))
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.UNCHANGED
    assert [r.method for r in replay.seen] == ["GET"]


async def test_a_missing_installation_is_a_manual_action() -> None:
    """A14: until the org installed the read-only App there is no installation id to write."""
    outcome = await offline_adapter(installation_id=None).ensure_credential(
        REPO_SCOPE, prior_for(REPO_SLOT)
    )
    assert outcome.change.state is ChangeState.NEEDS_MANUAL_ACTION
    assert outcome.change.manual_action == INSTALL_APP_ACTION
    assert outcome.credential is None
    assert outcome.state == prior_for(REPO_SLOT)


@pytest.mark.parametrize("missing", ["app_id", "private_key"])
async def test_the_read_only_app_is_required(missing: str) -> None:
    """Without the App id or key (A14) the credential cannot be written; no request is sent."""
    adapter = offline_adapter(**{missing: None})
    with pytest.raises(AdapterConfigurationError):
        await adapter.ensure_credential(REPO_SCOPE, prior_for(REPO_SLOT))


async def test_credentials_need_a_current_slot() -> None:
    """A state without a current slot is a non-retryable error."""
    with pytest.raises(AdapterError):
        await offline_adapter().ensure_credential(REPO_SCOPE, CredentialState(current=None))


def test_no_ci_outputs_are_produced() -> None:
    """Pull-based: nothing for the core to write to GitHub, for any credential."""
    adapter = offline_adapter()
    assert adapter.github_outputs(REPO_SCOPE, NoCiCredential("n/a")) == []
    assert adapter.github_outputs(ORG_SCOPE, IssuedSecret({"token": SecretStr("x")})) == []


# ---- rotation, deleting and listing credentials ----------------------------------------------
async def test_rotate_begin_is_refused() -> None:
    """Rotation is `none`: the core never calls it, and a direct call fails without a request."""
    with pytest.raises(AdapterError) as err:
        await offline_adapter().rotate_begin(REPO_SCOPE, prior_for(REPO_SLOT))
    assert not err.value.retryable


@pytest.mark.parametrize(
    ("scenario", "state"), [("delete_previous", "deleted"), ("delete_previous_absent", "unchanged")]
)
async def test_rotate_finish_deletes_only_previous_and_is_idempotent(
    scenario: str, state: str
) -> None:
    """A2: `previous` goes; `current` stays; an absent Secret is UNCHANGED."""
    replay, adapter = build("credentials.json", scenario)
    outcome = await adapter.rotate_finish(REPO_SCOPE, prior_for(REPO_SLOT, PREV_SLOT))
    replay.assert_exhausted()
    assert outcome.change.state.value == state
    assert outcome.state == CredentialState(current=REPO_SLOT)


async def test_rotate_finish_without_previous_makes_no_call() -> None:
    """Nothing to delete means no request."""
    outcome = await offline_adapter().rotate_finish(REPO_SCOPE, prior_for(REPO_SLOT))
    assert outcome.change.state is ChangeState.UNCHANGED


async def test_delete_credential_covers_current_and_previous_and_is_idempotent() -> None:
    """A2: both generations are deleted; an absent Secret is UNCHANGED; the state is emptied."""
    replay, adapter = build("credentials.json", "delete_both")
    outcome = await adapter.delete_credential(REPO_SCOPE, prior_for(REPO_SLOT, PREV_SLOT))
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.DELETED
    assert outcome.state == CredentialState(current=None)
    replay, adapter = build("credentials.json", "delete_absent")
    again = await adapter.delete_credential(REPO_SCOPE, prior_for(REPO_SLOT))
    replay.assert_exhausted()
    assert again.change.state is ChangeState.UNCHANGED


async def test_list_credentials_returns_only_this_tenants_names() -> None:
    """0028 invariant 22: names carry the tenant prefix; other tenants and junk are skipped."""
    replay, adapter = build("credentials.json", "list")
    listed = await adapter.list_credentials(TENANT)
    replay.assert_exhausted()
    assert {c.name for c in listed} == {CONTEXT["cred_pull"], CONTEXT["cred_org"]}
    assert all(c.ref == {"kind": "secret", "secret": c.name} for c in listed)
    assert all(c.name.startswith(f"tb-{TENANT_ID.hex}-") for c in listed)


async def test_list_credentials_may_return_no_items() -> None:
    """An empty or item-less listing is an empty result."""
    replay, adapter = build("credentials.json", "list_empty")
    assert await adapter.list_credentials(TENANT) == []
    replay.assert_exhausted()


# ---- errors and health (A5) ------------------------------------------------------------------
@pytest.mark.parametrize(
    ("scenario", "retryable"),
    [
        ("http_429", True),
        ("http_500", True),
        ("http_503", True),
        ("http_401", False),
        ("http_403", False),
        ("connect_error", True),
        ("read_timeout", True),
        ("bad_body", False),
    ],
)
async def test_errors_are_classified_and_leak_nothing(scenario: str, retryable: bool) -> None:
    """A5: 429/5xx/transport errors retry; auth and shape errors do not; no body or token leaks."""
    _, adapter = build("errors.json", scenario)
    with pytest.raises(AdapterError) as err:
        await adapter.ensure_project(TENANT, ProjectSpec("default"), None)
    assert err.value.retryable is retryable
    assert "leaky-body-secret" not in err.value.message
    assert SA_TOKEN not in err.value.message


async def test_health_check() -> None:
    """Reading one AppProject decides; an outage reports `ok=False` instead of raising."""
    replay, adapter = build("credentials.json", "health_ok")
    assert (await adapter.health_check()).ok
    replay.assert_exhausted()
    _, adapter = build("credentials.json", "health_down")
    health = await adapter.health_check()
    assert not health.ok
    assert health.detail is not None
    assert "503" in health.detail


def test_names_carry_the_full_tenant_id() -> None:
    """A4: every platform name embeds the whole tenant id."""
    for name in (CONTEXT["project"], CONTEXT["hook"], CONTEXT["cred_pull"]):
        assert name.startswith(f"tb-{TENANT_ID.hex}-")
