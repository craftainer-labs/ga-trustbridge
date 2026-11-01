"""Contract tests for the JFrog adapter: it replays recorded-style API exchanges (no network).

Each test names the ADR 0029 invariant it covers. The fixtures under `fixtures/` are built from
JFrog's documented request and response shapes; the module README lists what is unconfirmed.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import SecretStr

from tests.trustbridge.adapters.jfrog.replay import (
    ADMIN_TOKEN,
    BASE_URL,
    Replay,
    load_scenario,
)
from trustbridge.adapters.core import expected_claims
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
    OidcTrust,
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
    tenant_prefix,
)
from trustbridge.adapters.jfrog import JFrogAdapter
from trustbridge.adapters.jfrog.adapter import (
    AUDIENCE,
    mapping_priority,
    project_key_for,
    provider_name,
    trust_claims,
)
from trustbridge.adapters.loader import instantiate, validate_capabilities

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")
GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
GHES_ISSUER = "https://ghes.example/_services/token"
HOOK_URL = "https://tb.example/hooks/jfrog"
HOOK_SECRET = "whsec-dummy-value"  # noqa: S105 - a dummy used only in these fixtures


def make_tenant(kind: str = "github.com") -> Tenant:
    """Build the fixed tenant on a github.com or GHES host."""
    ghes = kind == "ghes"
    host = GitHubHost(
        name=kind,
        kind="ghes" if ghes else "github.com",
        api_url="https://ghes.example/api/v3" if ghes else "https://api.github.com",
        oidc_issuer=GHES_ISSUER if ghes else GITHUB_ISSUER,
        oidc_id_claims=True,
        registry_host="containers.ghes.example" if ghes else "ghcr.io",
    )
    return Tenant(id=TENANT_ID, github_host=host, org="acme")


TENANT = make_tenant()


def make_scope(
    permission: Permission,
    *,
    repo: int | None = 10,
    env: str | None = None,
    eligible: bool = False,
    tenant: Tenant = TENANT,
) -> CredentialScope:
    """Build a credential scope for owner 5."""
    scope = CredentialScope.build(
        tenant,
        repository_id=repo,
        repository_owner_id=5,
        environment=env,
        permission=permission,
    )
    return replace(scope, oidc_eligible=eligible)


PUSH = make_scope("push", env="prod")
PULL = make_scope("pull")
ORG_PULL = make_scope("pull", repo=None)
PROJECT = project_key_for(TENANT_ID, "default")
CONTEXT = {
    "project": PROJECT,
    "provider": provider_name(TENANT_ID),
    "cred_push": credential_name(TENANT_ID, PUSH.key, 2),
    "prev": credential_name(TENANT_ID, PUSH.key, 1),
    "cred_pull": credential_name(TENANT_ID, PULL.key, 1),
    "cred_org": credential_name(TENANT_ID, ORG_PULL.key, 1),
    "hook": resource_name(TENANT_ID, "webhook", HOOK_URL),
}
PUSH_SLOT = CredentialSlot(2, CONTEXT["cred_push"])
PREV_SLOT = CredentialSlot(1, CONTEXT["prev"], {"kind": "token", "token_id": "tid-prev"}, True)


def config(
    tenant: Tenant = TENANT, overrides: dict[str, SecretStr | None] | None = None
) -> AdapterConfig:
    """Build the tenant's connection data."""
    connection = {"base_url": SecretStr(BASE_URL), "hook-secret": SecretStr(HOOK_SECRET)}
    for key, value in (overrides or {}).items():
        if value is None:
            connection.pop(key, None)
        else:
            connection[key] = value
    return AdapterConfig(tenant, connection, SecretStr(ADMIN_TOKEN))


def build(file: str, scenario: str, tenant: Tenant = TENANT) -> tuple[Replay, JFrogAdapter]:
    """Return a replay of the scenario and an adapter wired to it."""
    replay = Replay(load_scenario(file, scenario, CONTEXT))
    adapter = JFrogAdapter(config(tenant), transport=replay.transport(), clock=lambda: NOW)
    return replay, adapter


def offline_adapter(**overrides: SecretStr | None) -> JFrogAdapter:
    """An adapter whose transport must never be used."""
    return JFrogAdapter(
        config(overrides=overrides), transport=Replay([]).transport(), clock=lambda: NOW
    )


# ---- capabilities and loading (A7) -----------------------------------------------------------
def test_capabilities_match_the_adr_matrix() -> None:
    """ADR 0029 matrix: OIDC on all kinds, single rotation, HMAC webhook, admin token."""
    adapter = offline_adapter()
    caps = adapter.capabilities()
    validate_capabilities(caps)
    assert caps.oidc_hosts == caps.github_hosts == frozenset({"github.com", "ghes", "ghe.com"})
    assert caps.rotation == "single"
    assert caps.needs_privileged_credential
    assert caps.webhook is not None
    assert caps.webhook.auth is WebhookAuth.HMAC
    assert caps.webhook.header == "X-JFrog-Event-Auth"
    assert caps.webhook.trigger_only
    assert isinstance(adapter, PlatformAdapter)


def test_the_loader_instantiates_with_the_config_only() -> None:
    """The loader calls `cls(config)`."""
    assert isinstance(instantiate(JFrogAdapter, config()), JFrogAdapter)


# ---- configuration errors --------------------------------------------------------------------
async def test_missing_admin_token_is_a_configuration_error() -> None:
    """No privileged credential: non-retryable configuration error, no request."""
    adapter = JFrogAdapter(
        AdapterConfig(TENANT, {"base_url": SecretStr(BASE_URL)}, None),
        transport=Replay([]).transport(),
    )
    with pytest.raises(AdapterConfigurationError):
        await adapter.ensure_project(TENANT, ProjectSpec("default"), None)
    health = await adapter.health_check()
    assert not health.ok
    assert health.detail is not None


@pytest.mark.parametrize("url", [None, "http://acme.jfrog.io", "https://acme.jfrog.io?x=1"])
async def test_the_base_url_must_be_an_https_url(url: str | None) -> None:
    """A missing, plain-http or query-carrying base URL is refused before any request."""
    adapter = offline_adapter(base_url=SecretStr(url) if url else None)
    with pytest.raises(AdapterConfigurationError):
        await adapter.ensure_project(TENANT, ProjectSpec("default"), None)


# ---- projects (A2, A3) -----------------------------------------------------------------------
async def test_ensure_project_creates_then_repeats_unchanged() -> None:
    """A2: the first call creates, the repeat (same prior, then a ref-less prior) is UNCHANGED."""
    spec = ProjectSpec("default", "images")
    replay, adapter = build("projects.json", "create")
    created = await adapter.ensure_project(TENANT, spec, None)
    replay.assert_exhausted()
    assert created.change.state is ChangeState.CREATED
    assert created.state == ResourceState(PROJECT, {"kind": "project", "project_key": PROJECT})
    for prior in (created.state, ResourceState(name=PROJECT)):
        replay, adapter = build("projects.json", "exists")
        again = await adapter.ensure_project(TENANT, spec, prior)
        replay.assert_exhausted()
        assert again.change.state is ChangeState.UNCHANGED
        assert again.state == created.state


async def test_ensure_project_looks_up_by_name_before_creating() -> None:
    """A3: an existing project is found by key; no POST is sent."""
    replay, adapter = build("projects.json", "exists")
    await adapter.ensure_project(TENANT, ProjectSpec("default", "images"), None)
    assert [r.method for r in replay.seen] == ["GET"]


async def test_ensure_project_updates_a_drifted_description() -> None:
    """Reconcile: a stale description is PUT back, keeping the other fields."""
    replay, adapter = build("projects.json", "drift")
    applied = await adapter.ensure_project(TENANT, ProjectSpec("default", "images"), None)
    replay.assert_exhausted()
    assert applied.change.state is ChangeState.UPDATED


async def test_ensure_project_tolerates_a_creation_race() -> None:
    """A3: a 409 on POST means a parallel worker created it; the result is UNCHANGED."""
    replay, adapter = build("projects.json", "race")
    applied = await adapter.ensure_project(TENANT, ProjectSpec("default", "images"), None)
    replay.assert_exhausted()
    assert applied.change.state is ChangeState.UNCHANGED
    assert applied.state is not None
    assert applied.state.name == PROJECT


# ---- repositories ----------------------------------------------------------------------------
PROJECT_REF = ProjectRef(TENANT, ResourceState(PROJECT, {"project_key": PROJECT}))
REPO_KEY = f"{PROJECT}-images"


@pytest.mark.parametrize("scenario", ["create", "create_after_404"])
async def test_ensure_repository_creates_a_project_repository(scenario: str) -> None:
    """The repo key carries the project prefix; an unknown repo answers 400 or 404."""
    replay, adapter = build("repositories.json", scenario)
    applied = await adapter.ensure_repository(PROJECT_REF, RepoSpec("images"), None)
    replay.assert_exhausted()
    assert applied.change.state is ChangeState.CREATED
    assert applied.state == ResourceState(
        REPO_KEY, {"kind": "repository", "repo_key": REPO_KEY, "project_key": PROJECT}
    )


async def test_ensure_repository_is_idempotent_by_name() -> None:
    """A2/A3: an existing repository (ref emptied) is UNCHANGED and not re-created."""
    replay, adapter = build("repositories.json", "exists")
    applied = await adapter.ensure_repository(
        PROJECT_REF, RepoSpec("images"), ResourceState(name=REPO_KEY)
    )
    replay.assert_exhausted()
    assert applied.change.state is ChangeState.UNCHANGED
    assert [r.method for r in replay.seen] == ["GET"]


async def test_ensure_repository_refuses_a_different_package_type() -> None:
    """A repository of another package type is not silently reused."""
    _, adapter = build("repositories.json", "wrong_package_type")
    with pytest.raises(AdapterError) as err:
        await adapter.ensure_repository(PROJECT_REF, RepoSpec("images"), None)
    assert not err.value.retryable


async def test_ensure_repository_converges_after_losing_a_race() -> None:
    """A3: a 'key already exists' answer to PUT is followed by a read and ends UNCHANGED."""
    replay, adapter = build("repositories.json", "race")
    applied = await adapter.ensure_repository(PROJECT_REF, RepoSpec("images"), None)
    replay.assert_exhausted()
    assert applied.change.state is ChangeState.UNCHANGED


async def test_ensure_repository_surfaces_a_rejected_creation() -> None:
    """A 400 that is not a race stays a non-retryable error."""
    _, adapter = build("repositories.json", "rejected")
    with pytest.raises(AdapterError) as err:
        await adapter.ensure_repository(PROJECT_REF, RepoSpec("images"), None)
    assert not err.value.retryable


@pytest.mark.parametrize("slug", ["Images", "a/b", "", "x" * 42])
async def test_ensure_repository_rejects_unusable_keys(slug: str) -> None:
    """Keys outside [a-z0-9-] or longer than 41 characters are a configuration error."""
    with pytest.raises(AdapterConfigurationError):
        await offline_adapter().ensure_repository(PROJECT_REF, RepoSpec(slug), None)


# ---- webhooks (decision 10: HMAC) ------------------------------------------------------------
TARGET = WebhookTarget(TENANT, HOOK_URL, (), "hook-secret")


async def test_ensure_webhook_creates_a_signed_subscription() -> None:
    """The subscription signs with the secret (`use_secret_for_signing`), i.e. HMAC."""
    replay, adapter = build("webhooks.json", "create")
    applied = await adapter.ensure_webhook(TARGET, None)
    replay.assert_exhausted()
    assert applied.change.state is ChangeState.CREATED
    assert applied.state == ResourceState(
        CONTEXT["hook"], {"kind": "webhook", "key": CONTEXT["hook"]}
    )
    assert HOOK_SECRET not in repr(applied.change)


async def test_ensure_webhook_is_idempotent() -> None:
    """A2/A3: an existing subscription with the same URL and events is UNCHANGED."""
    replay, adapter = build("webhooks.json", "exists")
    applied = await adapter.ensure_webhook(TARGET, ResourceState(name=CONTEXT["hook"]))
    replay.assert_exhausted()
    assert applied.change.state is ChangeState.UNCHANGED


@pytest.mark.parametrize(("scenario", "state"), [("drift", "updated"), ("race", "unchanged")])
async def test_ensure_webhook_converges(scenario: str, state: str) -> None:
    """Drifted events are PUT back; a 409 on POST is a lost race."""
    replay, adapter = build("webhooks.json", scenario)
    applied = await adapter.ensure_webhook(TARGET, None)
    replay.assert_exhausted()
    assert applied.change.state.value == state


async def test_ensure_webhook_needs_a_secret() -> None:
    """An unsigned webhook is refused; a secret_ref with no value is a configuration error."""
    with pytest.raises(AdapterConfigurationError):
        await offline_adapter().ensure_webhook(replace(TARGET, secret_ref=None), None)
    with pytest.raises(AdapterConfigurationError):
        await offline_adapter(**{"hook-secret": None}).ensure_webhook(TARGET, None)


# ---- deleting resources ----------------------------------------------------------------------
HOOK_STATE = ResourceState(CONTEXT["hook"], {"kind": "webhook", "key": CONTEXT["hook"]})
DELETE_CASES = [
    ("projects.json", ResourceState(PROJECT, {"kind": "project"})),
    ("projects.json", ResourceState(PROJECT)),
    ("repositories.json", ResourceState(REPO_KEY, {"kind": "repository"})),
    ("repositories.json", ResourceState(REPO_KEY)),
    ("webhooks.json", HOOK_STATE),
    ("webhooks.json", ResourceState(CONTEXT["hook"])),
]


@pytest.mark.parametrize(("file", "prior"), DELETE_CASES)
async def test_delete_removes_then_reports_absent_as_unchanged(
    file: str, prior: ResourceState
) -> None:
    """A2: delete is idempotent; the kind comes from `ref`, or from the name when `ref` is empty."""
    replay, adapter = build(file, "delete")
    deleted = await adapter.delete(prior)
    replay.assert_exhausted()
    assert deleted.change.state is ChangeState.DELETED
    assert deleted.state is None
    replay, adapter = build(file, "delete_absent")
    again = await adapter.delete(prior)
    replay.assert_exhausted()
    assert again.change.state is ChangeState.UNCHANGED


async def test_deleting_a_project_with_resources_is_a_non_retryable_error() -> None:
    """JFrog refuses to delete a non-empty project; retrying cannot help."""
    _, adapter = build("projects.json", "delete_not_empty")
    with pytest.raises(AdapterError) as err:
        await adapter.delete(ResourceState(PROJECT))
    assert not err.value.retryable


# ---- OIDC trust (A1, A6, decisions 5 and 12, open issue on mapping selection) ----------------
def prior_for(slot: CredentialSlot, previous: CredentialSlot | None = None) -> CredentialState:
    """The write-ahead state the core commits before a call."""
    return CredentialState(current=slot, previous=previous)


async def test_ensure_credential_creates_a_trust_with_exact_claims() -> None:
    """A6: the trust carries exactly the claims the core expects; the mapping requires them."""
    scope = replace(PUSH, oidc_eligible=True)
    replay, adapter = build("credentials.json", "trust_create")
    outcome = await adapter.ensure_credential(scope, prior_for(PUSH_SLOT, PREV_SLOT))
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.CREATED
    assert outcome.credential == OidcTrust(GITHUB_ISSUER, AUDIENCE, expected_claims(scope))
    assert outcome.state.current is not None
    assert outcome.state.current.delivered
    assert outcome.state.current.ref["kind"] == "mapping"
    assert outcome.state.previous == PREV_SLOT


async def test_trust_is_returned_on_every_outcome_and_outputs_are_pure() -> None:
    """A1: UNCHANGED (also with `ref` emptied) still returns the trust; outputs re-derive."""
    scope = replace(PUSH, oidc_eligible=True)
    replay, adapter = build("credentials.json", "trust_unchanged")
    outcome = await adapter.ensure_credential(scope, prior_for(PUSH_SLOT))
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.UNCHANGED
    assert isinstance(outcome.credential, OidcTrust)
    first = adapter.github_outputs(scope, outcome.credential)
    assert first == adapter.github_outputs(scope, outcome.credential)
    by_name = {o.name: o.value for o in first}
    assert by_name["JFROG_OIDC_PROVIDER"] == CONTEXT["provider"]
    assert by_name["JFROG_OIDC_AUDIENCE"] == AUDIENCE
    assert by_name["JFROG_URL"] == BASE_URL


@pytest.mark.parametrize("scenario", ["trust_update", "trust_provider_drift"])
async def test_trust_drift_is_reconciled(scenario: str) -> None:
    """Reconcile: a drifted mapping or issuer is PUT back and reported UPDATED."""
    replay, adapter = build("credentials.json", scenario)
    outcome = await adapter.ensure_credential(
        replace(PUSH, oidc_eligible=True), prior_for(PUSH_SLOT)
    )
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.UPDATED


async def test_trust_creation_race_converges() -> None:
    """A3: 409 on both POSTs means parallel workers created them; the trust is still returned."""
    replay, adapter = build("credentials.json", "trust_race")
    outcome = await adapter.ensure_credential(
        replace(PUSH, oidc_eligible=True), prior_for(PUSH_SLOT)
    )
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.UNCHANGED
    assert isinstance(outcome.credential, OidcTrust)


async def test_a_ghes_host_uses_the_generic_provider_with_its_issuer() -> None:
    """ADR matrix: GHES goes through a `Generic OpenID Connect` integration on the host's issuer."""
    tenant = make_tenant("ghes")
    scope = make_scope("push", env="prod", eligible=True, tenant=tenant)
    replay, adapter = build("credentials.json", "trust_create_ghes", tenant)
    outcome = await adapter.ensure_credential(scope, prior_for(PUSH_SLOT))
    replay.assert_exhausted()
    assert isinstance(outcome.credential, OidcTrust)
    assert outcome.credential.issuer == GHES_ISSUER


async def test_a_license_failure_on_oidc_is_a_non_retryable_error() -> None:
    """OIDC needs Enterprise X/+; a refusal ends the operation instead of retrying."""
    _, adapter = build("credentials.json", "trust_not_licensed")
    with pytest.raises(AdapterError) as err:
        await adapter.ensure_credential(replace(PUSH, oidc_eligible=True), prior_for(PUSH_SLOT))
    assert not err.value.retryable


@pytest.mark.parametrize(
    ("scope", "scenario", "name"),
    [
        (PULL, "trust_create_pull", "cred_pull"),
        (ORG_PULL, "trust_create_org_pull", "cred_org"),
    ],
)
async def test_mappings_for_other_scopes(scope: CredentialScope, scenario: str, name: str) -> None:
    """Pull and org-wide scopes get their own mapping, claims and (lower) priority."""
    replay, adapter = build("credentials.json", scenario)
    outcome = await adapter.ensure_credential(
        replace(scope, oidc_eligible=True), prior_for(CredentialSlot(1, CONTEXT[name]))
    )
    replay.assert_exhausted()
    assert isinstance(outcome.credential, OidcTrust)
    assert dict(outcome.credential.claims) == expected_claims(scope)


def test_mapping_selection_prefers_the_strongest_permission() -> None:
    """Open issue in ADR 0029: JFrog picks the lowest priority number among matching mappings.

    A workflow in a protected `prod` environment matches both the push mapping (environment
    claim) and the pull mapping (repository claims only); push has the lower number, so it wins.
    A workflow outside the environment matches only the pull mapping.
    """
    admin = make_scope("admin", env="stage")
    org_push_free = make_scope("pull", repo=None)
    assert mapping_priority(admin) < mapping_priority(PUSH) < mapping_priority(PULL)
    assert mapping_priority(PULL) < mapping_priority(org_push_free)
    push_claims = trust_claims(PUSH).items()
    pull_claims = trust_claims(PULL).items()
    assert pull_claims < push_claims  # a push token request also satisfies the pull mapping


@pytest.mark.parametrize("scope", [PUSH, PULL, ORG_PULL, make_scope("admin", env="stage")])
def test_trust_claims_are_exactly_the_core_claims(scope: CredentialScope) -> None:
    """A6: never `sub`, never the name claims; equal to the core's own derivation."""
    assert trust_claims(scope) == expected_claims(scope)
    assert not {"sub", "repository", "repository_owner"} & set(trust_claims(scope))


# ---- access tokens: issuance, replacement, idempotency (A2, A3, A8) --------------------------
async def test_ensure_credential_issues_a_secret_when_trust_is_not_offered() -> None:
    """Not eligible: an access token, undelivered, with the id in `ref`."""
    replay, adapter = build("credentials.json", "token_issue")
    outcome = await adapter.ensure_credential(PUSH, prior_for(PUSH_SLOT))
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.CREATED
    assert isinstance(outcome.credential, IssuedSecret)
    assert outcome.credential.values["token"].get_secret_value() == "eyJ-dummy-access-token"
    assert outcome.credential.expires_at == NOW + timedelta(seconds=7776000)
    assert outcome.state.current is not None
    assert not outcome.state.current.delivered
    assert outcome.state.current.ref == {"kind": "token", "token_id": "tid-new"}
    assert "eyJ-dummy" not in repr(outcome.change)


async def test_repeating_an_undelivered_issue_replaces_the_token_under_the_same_name() -> None:
    """A2: the repeat creates a new value, revokes the old one, and reports UPDATED."""
    replay, adapter = build("credentials.json", "token_reissue")
    outcome = await adapter.ensure_credential(PUSH, prior_for(PUSH_SLOT))
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.UPDATED
    assert [r.method for r in replay.seen] == ["GET", "POST", "DELETE"]
    assert replay.seen[-1].url.path.endswith("/tokens/tid-old")


async def test_a_delivered_secret_is_unchanged_and_not_reissued() -> None:
    """A2: a delivered secret that still exists yields UNCHANGED and `credential=None`."""
    replay, adapter = build("credentials.json", "token_unchanged")
    slot = replace(PUSH_SLOT, delivered=True)
    outcome = await adapter.ensure_credential(PUSH, prior_for(slot))
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.UNCHANGED
    assert outcome.credential is None
    assert outcome.state == prior_for(slot)


async def test_a_delivered_secret_that_vanished_is_issued_again() -> None:
    """The lookup finds nothing, so a new token is created and must be delivered again."""
    replay, adapter = build("credentials.json", "token_issue")
    outcome = await adapter.ensure_credential(PUSH, prior_for(replace(PUSH_SLOT, delivered=True)))
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.CREATED
    assert outcome.state.current is not None
    assert not outcome.state.current.delivered


async def test_a_lost_response_is_retried_to_one_token_per_name() -> None:
    """A8/A10: the first POST answers with a dropped connection (retryable); the retry finds the
    token the platform created, replaces it, and one token per name remains."""
    exchanges = load_scenario("credentials.json", "token_issue", CONTEXT)
    exchanges[1]["response"] = {"status": 0, "raise": "ReadTimeout"}
    replay = Replay(exchanges)
    adapter = JFrogAdapter(config(), transport=replay.transport(), clock=lambda: NOW)
    with pytest.raises(AdapterError) as err:
        await adapter.ensure_credential(PUSH, prior_for(PUSH_SLOT))
    assert err.value.retryable
    replay, adapter = build("credentials.json", "token_reissue")
    outcome = await adapter.ensure_credential(PUSH, prior_for(PUSH_SLOT))
    replay.assert_exhausted()
    assert isinstance(outcome.credential, IssuedSecret)


# ---- rotation (single) and its rules (A9) ----------------------------------------------------
async def test_rotate_begin_replaces_in_place_and_returns_no_previous() -> None:
    """`rotation="single"`: same name, new value, `previous=None` even if the prior had one."""
    replay, adapter = build("credentials.json", "rotate_begin")
    prior = prior_for(replace(PUSH_SLOT, delivered=True), PREV_SLOT)
    outcome = await adapter.rotate_begin(PUSH, prior)
    replay.assert_exhausted()
    assert isinstance(outcome.credential, IssuedSecret)
    assert outcome.state.previous is None
    assert outcome.state.current is not None
    assert outcome.state.current.name == PUSH_SLOT.name
    assert not outcome.state.current.delivered


async def test_rotate_begin_refuses_a_trust() -> None:
    """An OIDC trust has no secret to rotate (the core never asks)."""
    slot = replace(PUSH_SLOT, ref={"kind": "mapping"}, delivered=True)
    with pytest.raises(AdapterError) as err:
        await offline_adapter().rotate_begin(PUSH, prior_for(slot))
    assert not err.value.retryable


async def test_rotate_finish_deletes_only_previous_and_is_idempotent() -> None:
    """A9: only `previous` is deleted (no call touches `current`); a repeat is UNCHANGED."""
    prior = prior_for(replace(PUSH_SLOT, delivered=True), PREV_SLOT)
    replay, adapter = build("credentials.json", "rotate_finish")
    outcome = await adapter.rotate_finish(PUSH, prior)
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.DELETED
    assert outcome.state == CredentialState(current=prior.current)
    assert outcome.credential is None
    replay, adapter = build("credentials.json", "rotate_finish_absent")
    again = await adapter.rotate_finish(PUSH, prior)
    replay.assert_exhausted()
    assert again.change.state is ChangeState.UNCHANGED


async def test_rotate_finish_without_previous_makes_no_call() -> None:
    """Nothing to delete: UNCHANGED without touching the platform."""
    outcome = await offline_adapter().rotate_finish(PUSH, prior_for(PUSH_SLOT))
    assert outcome.change.state is ChangeState.UNCHANGED


# ---- deleting credentials and the orphan sweep (A10, 0028 invariant 22) ----------------------
@pytest.mark.parametrize(
    ("scenario", "ref", "state"),
    [
        ("delete_token", {"kind": "token", "token_id": "tid-old"}, "deleted"),
        ("delete_token", {}, "deleted"),
        ("delete_token_absent", {"kind": "token"}, "unchanged"),
        ("delete_unknown_kind", {}, "deleted"),
        ("delete_unknown_kind_absent", {}, "unchanged"),
        ("delete_mapping", {"kind": "mapping", "provider": CONTEXT["provider"]}, "deleted"),
    ],
)
async def test_delete_credential_removes_by_name_and_is_idempotent(
    scenario: str, ref: dict[str, str], state: str
) -> None:
    """A2/A10: deletion needs only `name` and `ref`; an absent object is UNCHANGED."""
    # `delete_token` with an empty ref also probes for a mapping; the scenarios differ accordingly.
    name = "delete_unknown_kind" if scenario == "delete_token" and not ref else scenario
    replay, adapter = build("credentials.json", name)
    slot = replace(PUSH_SLOT, ref=ref)
    outcome = await adapter.delete_credential(PUSH, prior_for(slot))
    replay.assert_exhausted()
    assert outcome.change.state.value == state
    assert outcome.state == CredentialState(current=None)
    assert outcome.credential is None


async def test_delete_credential_covers_current_and_previous() -> None:
    """A switch revokes the old slot: both slots of `prior` are cleaned up."""
    replay, adapter = build("credentials.json", "delete_both_slots")
    slot = replace(PUSH_SLOT, ref={"kind": "token"})
    prev = replace(PREV_SLOT, ref={"kind": "token"})
    outcome = await adapter.delete_credential(PUSH, prior_for(slot, prev))
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.DELETED


async def test_list_credentials_returns_only_this_tenants_names() -> None:
    """The orphan sweep sees this tenant's tokens and mappings, never another tenant's."""
    replay, adapter = build("credentials.json", "list")
    listed = await adapter.list_credentials(TENANT)
    replay.assert_exhausted()
    assert {(c.name, c.ref["kind"]) for c in listed} == {
        (CONTEXT["cred_push"], "token"),
        (CONTEXT["cred_pull"], "mapping"),
    }
    assert all(c.name.startswith(tenant_prefix(TENANT_ID)) for c in listed)


async def test_list_credentials_without_a_provider() -> None:
    """No OIDC integration yet: no mappings, no error."""
    replay, adapter = build("credentials.json", "list_no_provider")
    assert await adapter.list_credentials(TENANT) == []
    replay.assert_exhausted()


async def test_listed_credentials_can_be_deleted_by_name_and_ref_alone() -> None:
    """0028 invariant 22: `delete_credential` works from a `ListedCredential`'s name and ref."""
    replay, adapter = build("credentials.json", "list")
    listed = [c for c in await adapter.list_credentials(TENANT) if c.ref["kind"] == "mapping"]
    replay.assert_exhausted()
    replay, adapter = build("credentials.json", "delete_mapping")
    slot = CredentialSlot(1, listed[0].name, listed[0].ref)
    replay.exchanges[0]["request"]["path"] = replay.exchanges[0]["request"]["path"].replace(
        CONTEXT["cred_push"], listed[0].name
    )
    outcome = await adapter.delete_credential(PULL, prior_for(slot))
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.DELETED


# ---- outputs, names, secrecy, errors, health (A4, A5) ----------------------------------------
def test_secret_outputs_carry_the_token_as_a_secret() -> None:
    """The token is a GitHub secret (never a variable); the URL is a variable at the scope."""
    adapter = offline_adapter()
    secret = IssuedSecret({"token": SecretStr("tok-value")})
    outputs = adapter.github_outputs(PUSH, secret)
    kinds = {o.name: o.kind for o in outputs}
    assert kinds == {"JFROG_URL": "variable", "JFROG_ACCESS_TOKEN": "secret"}
    assert all(o.repository_id == 10 and o.environment == "prod" for o in outputs)
    token = next(o.value for o in outputs if o.kind == "secret")
    assert isinstance(token, SecretStr)
    assert token.get_secret_value() == "tok-value"
    assert adapter.github_outputs(ORG_PULL, secret)[0].repository_id is None
    assert adapter.github_outputs(PUSH, NoCiCredential("n/a")) == []


def test_names_carry_the_full_tenant_id() -> None:
    """A4: every platform name that is not a project key embeds the whole tenant id."""
    assert CONTEXT["cred_push"].startswith(f"tb-{TENANT_ID.hex}-")
    assert CONTEXT["provider"].startswith(f"tb-{TENANT_ID.hex}-")
    assert CONTEXT["hook"].startswith(f"tb-{TENANT_ID.hex}-")
    assert project_key_for(TENANT_ID, "a") != project_key_for(UUID(int=2), "a")
    assert project_key_for(TENANT_ID, "a").isalnum()


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
    assert ADMIN_TOKEN not in err.value.message


async def test_health_check() -> None:
    """The ping decides; an outage reports `ok=False` instead of raising."""
    replay, adapter = build("credentials.json", "health_ok")
    assert (await adapter.health_check()).ok
    replay.assert_exhausted()
    _, adapter = build("credentials.json", "health_down")
    health = await adapter.health_check()
    assert not health.ok
    assert health.detail is not None
    assert "503" in health.detail
