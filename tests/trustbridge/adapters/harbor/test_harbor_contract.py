"""Contract tests for the Harbor / MSR adapter: it replays recorded-style API exchanges.

Each test names the ADR 0029 invariant it covers. The fixtures under `fixtures/` are built from
Harbor's published swagger definition; the module README lists what is unconfirmed.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import SecretStr

from tests.trustbridge.adapters.harbor.replay import (
    ADMIN_SECRET,
    ADMIN_USER,
    BASE_URL,
    Replay,
    load_scenario,
)
from trustbridge.adapters.harbor import HarborAdapter
from trustbridge.adapters.harbor.adapter import project_name_for, robot_access
from trustbridge.adapters.harbor.client import HarborClient
from trustbridge.adapters.interface import (
    AdapterConfig,
    AdapterConfigurationError,
    AdapterError,
    ChangeState,
    CredentialScope,
    CredentialSlot,
    CredentialState,
    GitHubHost,
    GitHubOutput,
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
from trustbridge.adapters.loader import instantiate, validate_capabilities

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")
GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
HOOK_URL = "https://tb.example/hooks/harbor"
REFRESHED = "refreshed-secret-value"
HOOK_SECRET = "whsec-dummy-value"  # noqa: S105 - a dummy used only in these fixtures

HOST = GitHubHost(
    name="github.com",
    kind="github.com",
    api_url="https://api.github.com",
    oidc_issuer=GITHUB_ISSUER,
    oidc_id_claims=True,
    registry_host="ghcr.io",
)
TENANT = Tenant(id=TENANT_ID, github_host=HOST, org="acme")


def make_scope(
    permission: Permission,
    *,
    repo: int | None = 10,
    env: str | None = None,
    platform_repository_key: str | None = None,
) -> CredentialScope:
    """Build a credential scope for owner 5."""
    scope = CredentialScope.build(
        TENANT,
        repository_id=repo,
        repository_owner_id=5,
        environment=env,
        permission=permission,
    )
    return replace(scope, platform_repository_key=platform_repository_key)


PUSH = make_scope("push", env="prod")
PROJECT = project_name_for(TENANT_ID, "default")
CRED = credential_name(TENANT_ID, PUSH.key, 2)
PREV = credential_name(TENANT_ID, PUSH.key, 1)
CONTEXT = {
    "project": PROJECT,
    "cred": CRED,
    "prev": PREV,
    "prefix": tenant_prefix(TENANT_ID),
    "hook": resource_name(TENANT_ID, "webhook", HOOK_URL),
    "hook_url": HOOK_URL,
    "hook_secret": HOOK_SECRET,
}
HOOK = CONTEXT["hook"]
SLOT = CredentialSlot(2, CRED)
DELIVERED_SLOT = CredentialSlot(2, CRED, {"kind": "robot", "robot_id": "11"}, True)
PREV_SLOT = CredentialSlot(1, PREV, {"kind": "robot", "robot_id": "10", "project": PROJECT}, True)
TARGET = WebhookTarget(TENANT, HOOK_URL, (), "hook-secret")


def config(overrides: dict[str, SecretStr | None] | None = None) -> AdapterConfig:
    """Build the tenant's connection data."""
    connection = {
        "base_url": SecretStr(BASE_URL),
        "username": SecretStr(ADMIN_USER),
        "hook-secret": SecretStr(HOOK_SECRET),
    }
    for key, value in (overrides or {}).items():
        if value is None:
            connection.pop(key, None)
        else:
            connection[key] = value
    return AdapterConfig(TENANT, connection, SecretStr(ADMIN_SECRET))


def build(
    file: str, scenario: str, overrides: dict[str, SecretStr | None] | None = None
) -> tuple[Replay, HarborAdapter]:
    """Return a replay of the scenario and an adapter wired to it."""
    replay = Replay(load_scenario(file, scenario, CONTEXT))
    adapter = HarborAdapter(config(overrides), transport=replay.transport(), clock=lambda: NOW)
    return replay, adapter


def offline_adapter(**overrides: SecretStr | None) -> HarborAdapter:
    """An adapter whose transport must never be used."""
    return HarborAdapter(config(overrides), transport=Replay([]).transport(), clock=lambda: NOW)


def state_of(
    slot: CredentialSlot = SLOT, previous: CredentialSlot | None = None
) -> CredentialState:
    """Build a persisted credential state."""
    return CredentialState(current=slot, previous=previous)


def secret_values(issued: object) -> dict[str, str]:
    """Reveal an issued secret's values for assertions."""
    assert isinstance(issued, IssuedSecret)
    return {k: v.get_secret_value() for k, v in issued.values.items()}


# ---- capabilities and loading (A7) -----------------------------------------------------------
def test_capabilities_match_the_adr_matrix() -> None:
    """ADR 0029 matrix: no OIDC, in-place rotation, static-header webhook, admin credential."""
    adapter = offline_adapter()
    caps = adapter.capabilities()
    validate_capabilities(caps)
    assert not caps.oidc
    assert caps.oidc_hosts == frozenset()
    assert caps.github_hosts == frozenset({"github.com", "ghes", "ghe.com"})
    assert caps.rotation == "single"
    assert caps.needs_privileged_credential
    assert caps.webhook is not None
    assert caps.webhook.auth is WebhookAuth.STATIC_HEADER
    assert caps.webhook.header == "Authorization"
    assert caps.webhook.trigger_only
    assert isinstance(adapter, PlatformAdapter)


def test_the_loader_instantiates_with_the_config_only() -> None:
    """The loader calls `cls(config)`."""
    assert isinstance(instantiate(HarborAdapter, config()), HarborAdapter)


# ---- configuration errors --------------------------------------------------------------------
async def test_missing_admin_credential_is_a_configuration_error() -> None:
    """No privileged credential: non-retryable configuration error, health reports it."""
    adapter = HarborAdapter(
        AdapterConfig(TENANT, {"base_url": SecretStr(BASE_URL)}, None),
        transport=Replay([]).transport(),
    )
    with pytest.raises(AdapterConfigurationError):
        await adapter.ensure_project(TENANT, ProjectSpec("default"), None)
    health = await adapter.health_check()
    assert not health.ok
    assert health.detail is not None


async def test_missing_username_is_a_configuration_error() -> None:
    """Harbor basic auth needs a user name next to the secret."""
    adapter = offline_adapter(username=None)
    with pytest.raises(AdapterConfigurationError):
        await adapter.ensure_project(TENANT, ProjectSpec("default"), None)


@pytest.mark.parametrize("url", [None, "http://harbor.example", "https://harbor.example?x=1"])
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
    assert created.state == ResourceState(PROJECT, {"kind": "project", "project_name": PROJECT})
    for prior in (created.state, ResourceState(name=PROJECT)):
        replay, adapter = build("projects.json", "exists")
        again = await adapter.ensure_project(TENANT, spec, prior)
        replay.assert_exhausted()
        assert again.change.state is ChangeState.UNCHANGED
        assert again.state == created.state


async def test_ensure_project_looks_up_by_name_before_creating() -> None:
    """A3: an existing project is found by name; no POST is sent."""
    replay, adapter = build("projects.json", "exists")
    await adapter.ensure_project(TENANT, ProjectSpec("default"), None)
    assert [r.method for r in replay.seen] == ["GET"]


async def test_ensure_project_tolerates_a_creation_race() -> None:
    """A3: a 409 on POST means a parallel worker created it; the result is UNCHANGED."""
    replay, adapter = build("projects.json", "race")
    applied = await adapter.ensure_project(TENANT, ProjectSpec("default"), None)
    replay.assert_exhausted()
    assert applied.change.state is ChangeState.UNCHANGED


async def test_delete_project_then_absent() -> None:
    """A2: deleting removes the project; a project that is already gone is UNCHANGED."""
    prior = ResourceState(PROJECT, {"kind": "project"})
    replay, adapter = build("projects.json", "delete")
    gone = await adapter.delete(prior)
    replay.assert_exhausted()
    assert (gone.change.state, gone.state) == (ChangeState.DELETED, None)
    replay, adapter = build("projects.json", "delete_absent")
    again = await adapter.delete(prior)
    replay.assert_exhausted()
    assert again.change.state is ChangeState.UNCHANGED


async def test_delete_project_with_repositories_fails_without_retry() -> None:
    """Harbor answers 412 for a project that still holds repositories: not retryable."""
    _, adapter = build("projects.json", "delete_not_empty")
    with pytest.raises(AdapterError) as info:
        await adapter.delete(ResourceState(PROJECT, {"kind": "project"}))
    assert not info.value.retryable


async def test_delete_unknown_kind_is_refused() -> None:
    """A state with no known kind is a non-retryable error and sends nothing."""
    with pytest.raises(AdapterError) as info:
        await offline_adapter().delete(ResourceState("x", {"kind": "mystery"}))
    assert not info.value.retryable


# ---- repositories (Harbor has no create-repository API) ----------------------------------------
async def test_ensure_repository_names_without_any_request() -> None:
    """A2: the repository is `<project>/<key>`; nothing is sent, and the change is UNCHANGED."""
    adapter = offline_adapter()
    project = ResourceState(PROJECT, {"kind": "project", "project_name": PROJECT})
    applied = await adapter.ensure_repository(
        ProjectRef(TENANT, project), RepoSpec("team/app"), None
    )
    name = f"{PROJECT}/team/app"
    assert applied.change.state is ChangeState.UNCHANGED
    assert applied.state == ResourceState(
        name, {"kind": "repository", "repo_name": name, "project_name": PROJECT}
    )
    again = await adapter.ensure_repository(
        ProjectRef(TENANT, ResourceState(PROJECT)), RepoSpec("other"), applied.state
    )
    assert again.state == applied.state


@pytest.mark.parametrize("key", ["Upper", "has space", "a//b", "", "x" * 201])
async def test_ensure_repository_rejects_unusable_keys(key: str) -> None:
    """A key Harbor would not accept as a repository path is a configuration error."""
    with pytest.raises(AdapterConfigurationError):
        await offline_adapter().ensure_repository(
            ProjectRef(TENANT, ResourceState(PROJECT)), RepoSpec(key), None
        )


async def test_delete_repository_never_touches_the_platform() -> None:
    """Deleting a repository resource is a no-op: the adapter never created one."""
    applied = await offline_adapter().delete(
        ResourceState(f"{PROJECT}/app", {"kind": "repository"})
    )
    assert (applied.change.state, applied.state) == (ChangeState.UNCHANGED, None)


# ---- webhooks --------------------------------------------------------------------------------
async def test_ensure_webhook_creates_then_repeats_unchanged() -> None:
    """A2: create sends the policy with the secret as the auth header; the repeat is UNCHANGED."""
    replay, adapter = build("webhooks.json", "create")
    created = await adapter.ensure_webhook(TARGET, None)
    replay.assert_exhausted()
    assert created.change.state is ChangeState.CREATED
    assert created.state == ResourceState(
        HOOK, {"kind": "webhook", "project": PROJECT, "policy": HOOK}
    )
    replay, adapter = build("webhooks.json", "exists")
    again = await adapter.ensure_webhook(TARGET, created.state)
    replay.assert_exhausted()
    assert again.change.state is ChangeState.UNCHANGED


async def test_ensure_webhook_ignores_other_policies_and_missing_project() -> None:
    """A3: only a policy with the deterministic name counts; a 404 listing means none."""
    for scenario in ("create_after_other_policy", "create_project_missing"):
        replay, adapter = build("webhooks.json", scenario)
        applied = await adapter.ensure_webhook(TARGET, None)
        replay.assert_exhausted()
        assert applied.change.state is ChangeState.CREATED


async def test_ensure_webhook_uses_the_given_events() -> None:
    """The events of the target replace the default."""
    replay, adapter = build("webhooks.json", "events")
    target = replace(TARGET, events=("PUSH_ARTIFACT", "DELETE_ARTIFACT"))
    await adapter.ensure_webhook(target, None)
    replay.assert_exhausted()


@pytest.mark.parametrize("scenario", ["drift_url", "drift_events", "drift_disabled"])
async def test_ensure_webhook_updates_a_drifted_policy(scenario: str) -> None:
    """Reconcile: a changed URL, event list or disabled flag is PUT back."""
    replay, adapter = build("webhooks.json", scenario)
    applied = await adapter.ensure_webhook(TARGET, ResourceState(HOOK, {"kind": "webhook"}))
    replay.assert_exhausted()
    assert applied.change.state is ChangeState.UPDATED


async def test_ensure_webhook_tolerates_a_creation_race() -> None:
    """A3: a 409 on POST means a parallel worker created it."""
    replay, adapter = build("webhooks.json", "race")
    applied = await adapter.ensure_webhook(TARGET, None)
    replay.assert_exhausted()
    assert applied.change.state is ChangeState.UNCHANGED


async def test_ensure_webhook_uses_the_project_in_its_prior_ref() -> None:
    """A prior that names another project keeps that project (the fixture is project-keyed)."""
    replay, adapter = build("webhooks.json", "exists")
    prior = ResourceState(HOOK, {"kind": "webhook", "project": PROJECT})
    applied = await adapter.ensure_webhook(TARGET, prior)
    replay.assert_exhausted()
    assert applied.state is not None
    assert applied.state.ref["project"] == PROJECT
    assert applied.state.name == HOOK


async def test_ensure_webhook_requires_a_secret() -> None:
    """A unsigned or secret-less webhook is a configuration error before any request."""
    adapter = offline_adapter()
    with pytest.raises(AdapterConfigurationError):
        await adapter.ensure_webhook(replace(TARGET, secret_ref=None), None)
    with pytest.raises(AdapterConfigurationError):
        await adapter.ensure_webhook(WebhookTarget(TENANT, HOOK_URL, (), "absent"), None)


async def test_a_policy_without_an_id_is_an_error() -> None:
    """A listed policy that needs updating or deleting but has no integer id is refused."""
    _, adapter = build("webhooks.json", "no_id")
    with pytest.raises(AdapterError) as info:
        await adapter.ensure_webhook(TARGET, None)
    assert not info.value.retryable
    _, adapter = build("webhooks.json", "no_id")
    with pytest.raises(AdapterError):
        await adapter.delete(ResourceState(HOOK, {"kind": "webhook"}))


async def test_a_non_list_policy_listing_is_an_error() -> None:
    """A listing that is not an array is a non-retryable error."""
    _, adapter = build("webhooks.json", "bad_listing")
    with pytest.raises(AdapterError) as info:
        await adapter.ensure_webhook(TARGET, None)
    assert not info.value.retryable


async def test_delete_webhook_variants() -> None:
    """A2: delete removes the policy found by name; unlisted or already gone is UNCHANGED."""
    prior = ResourceState(HOOK, {"kind": "webhook", "project": PROJECT, "policy": HOOK})
    replay, adapter = build("webhooks.json", "delete")
    gone = await adapter.delete(prior)
    replay.assert_exhausted()
    assert (gone.change.state, gone.state) == (ChangeState.DELETED, None)
    for scenario in ("delete_unlisted", "delete_race"):
        replay, adapter = build("webhooks.json", scenario)
        again = await adapter.delete(ResourceState(HOOK, {"kind": "webhook"}))
        replay.assert_exhausted()
        assert again.change.state is ChangeState.UNCHANGED


# ---- credentials: grants (user decision: project-wide on Harbor) -------------------------------
def test_robot_access_maps_permissions() -> None:
    """pull reads; push adds pull; admin adds delete (all on the `repository` resource)."""
    assert [a["action"] for a in robot_access("pull")] == ["pull"]
    assert [a["action"] for a in robot_access("push")] == ["pull", "push"]
    assert [a["action"] for a in robot_access("admin")] == ["pull", "push", "delete"]
    assert {a["resource"] for a in robot_access("admin")} == {"repository"}
    assert {a["effect"] for a in robot_access("admin")} == {"allow"}


@pytest.mark.parametrize("repository_key", [None, "images"])
@pytest.mark.parametrize("permission", ["pull", "push", "admin"])
async def test_ensure_credential_grants_the_whole_project_in_both_scopes(
    permission: Permission, repository_key: str | None
) -> None:
    """A6: a project-level robot with the same permissions, with or without a repository key.

    Harbor robot permissions stop at project scope, so `platform_repository_key` set or unset
    gives the identical request body (the fixture pins the body exactly).
    """
    scope = make_scope(permission, env="prod", platform_repository_key=repository_key)
    replay, adapter = build("credentials.json", f"create_{permission}")
    outcome = await adapter.ensure_credential(scope, state_of())
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.CREATED
    assert isinstance(outcome.credential, IssuedSecret)
    assert secret_values(outcome.credential) == {
        "username": f"robot${PROJECT}+{CRED}",
        "password": "robot-secret-value",
    }
    assert outcome.credential.expires_at == NOW + timedelta(days=90)
    assert outcome.state.current == replace(
        SLOT, ref={"kind": "robot", "robot_id": "11", "project": PROJECT}, delivered=False
    )


async def test_ensure_credential_ignores_oidc_eligibility() -> None:
    """Harbor has no OIDC trust: an `oidc_eligible` scope still gets a secret robot."""
    scope = replace(PUSH, oidc_eligible=True)
    replay, adapter = build("credentials.json", "create_push")
    outcome = await adapter.ensure_credential(scope, state_of())
    replay.assert_exhausted()
    assert isinstance(outcome.credential, IssuedSecret)


async def test_ensure_credential_with_a_configured_project_key() -> None:
    """The optional `project_key` connection entry names the project robots belong to."""
    context = {**CONTEXT, "project": "custom"}
    replay = Replay(load_scenario("credentials.json", "create_push", context))
    adapter = HarborAdapter(
        config({"project_key": SecretStr("custom")}),
        transport=replay.transport(),
        clock=lambda: NOW,
    )
    outcome = await adapter.ensure_credential(PUSH, state_of())
    replay.assert_exhausted()
    assert outcome.state.current is not None
    assert outcome.state.current.ref["project"] == "custom"


async def test_ensure_credential_is_idempotent_once_delivered() -> None:
    """A2: a delivered slot whose robot exists issues nothing and changes nothing."""
    replay, adapter = build("credentials.json", "exists_delivered")
    prior = state_of(DELIVERED_SLOT)
    outcome = await adapter.ensure_credential(PUSH, prior)
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.UNCHANGED
    assert outcome.state == prior
    assert outcome.credential is None


@pytest.mark.parametrize(
    ("scenario", "expires_at"),
    [("exists_undelivered", NOW + timedelta(days=90)), ("exists_never_expires", None)],
)
async def test_ensure_credential_reissues_an_undelivered_secret(
    scenario: str, expires_at: datetime | None
) -> None:
    """A3: a robot that exists but whose secret was never delivered gets a refreshed secret."""
    replay, adapter = build("credentials.json", scenario)
    outcome = await adapter.ensure_credential(PUSH, state_of(SLOT, PREV_SLOT))
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.UPDATED
    assert secret_values(outcome.credential)["password"] == REFRESHED
    assert isinstance(outcome.credential, IssuedSecret)
    assert outcome.credential.expires_at == expires_at
    assert outcome.state.previous is None


async def test_a_robot_of_another_project_does_not_count() -> None:
    """A3: a robot with the same name in a different project is not ours; a new one is created."""
    replay, adapter = build("credentials.json", "exists_other_project")
    outcome = await adapter.ensure_credential(PUSH, state_of(DELIVERED_SLOT))
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.CREATED


async def test_a_creation_conflict_is_retryable() -> None:
    """A robot created in parallel after the lookup: retry, which then finds it."""
    _, adapter = build("credentials.json", "create_conflict")
    with pytest.raises(AdapterError) as info:
        await adapter.ensure_credential(PUSH, state_of())
    assert info.value.retryable


@pytest.mark.parametrize(
    ("scenario", "retryable"),
    [("create_bad_body", False), ("bad_listing", False), ("bad_listing_text", False)],
)
async def test_unexpected_credential_responses_are_errors(scenario: str, retryable: bool) -> None:
    """A5: a malformed answer is an error without any body in the message."""
    _, adapter = build("credentials.json", scenario)
    with pytest.raises(AdapterError) as info:
        await adapter.ensure_credential(PUSH, state_of())
    assert info.value.retryable is retryable


async def test_a_listed_robot_without_an_id_is_an_error() -> None:
    """A matching robot that has no integer id cannot be addressed."""
    _, adapter = build("credentials.json", "robot_without_id")
    with pytest.raises(AdapterError):
        await adapter.ensure_credential(PUSH, state_of())


@pytest.mark.parametrize("scenario", ["refresh_bad", "refresh_not_json"])
async def test_a_bad_secret_refresh_response_is_an_error(scenario: str) -> None:
    """A refresh that returns no secret is a non-retryable error."""
    _, adapter = build("credentials.json", scenario)
    with pytest.raises(AdapterError) as info:
        await adapter.ensure_credential(PUSH, state_of())
    assert not info.value.retryable


async def test_a_credential_needs_a_current_slot() -> None:
    """No current slot is a non-retryable error."""
    with pytest.raises(AdapterError):
        await offline_adapter().ensure_credential(PUSH, CredentialState(current=None))


# ---- rotation (single, in place) ------------------------------------------------------------
async def test_rotate_begin_refreshes_the_secret_in_place() -> None:
    """A2/A3: the same robot gets a new secret; `previous` is dropped (single rotation)."""
    replay, adapter = build("credentials.json", "rotate_existing")
    outcome = await adapter.rotate_begin(PUSH, state_of(DELIVERED_SLOT, PREV_SLOT))
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.UPDATED
    assert secret_values(outcome.credential)["password"] == REFRESHED
    assert outcome.state.previous is None
    assert outcome.state.current is not None
    assert not outcome.state.current.delivered


async def test_rotate_begin_creates_a_missing_robot() -> None:
    """A robot deleted out of band is created again when rotation starts."""
    replay, adapter = build("credentials.json", "rotate_absent")
    outcome = await adapter.rotate_begin(PUSH, state_of(DELIVERED_SLOT))
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.CREATED


async def test_rotate_finish_deletes_only_a_distinct_previous() -> None:
    """A3: `previous` goes; absent and 404 are UNCHANGED; nothing to do sends nothing."""
    replay, adapter = build("credentials.json", "finish")
    done = await adapter.rotate_finish(PUSH, state_of(SLOT, PREV_SLOT))
    replay.assert_exhausted()
    assert done.change.state is ChangeState.DELETED
    assert done.state == CredentialState(current=SLOT)
    for scenario in ("finish_absent", "finish_race"):
        replay, adapter = build("credentials.json", scenario)
        again = await adapter.rotate_finish(PUSH, state_of(SLOT, PREV_SLOT))
        replay.assert_exhausted()
        assert again.change.state is ChangeState.UNCHANGED
    offline = offline_adapter()
    for state in (state_of(SLOT), state_of(SLOT, replace(SLOT, generation=1))):
        untouched = await offline.rotate_finish(PUSH, state)
        assert untouched.change.state is ChangeState.UNCHANGED


async def test_rotate_finish_without_a_current_slot_deletes_previous() -> None:
    """A previous slot with no current one is still deleted."""
    replay, adapter = build("credentials.json", "finish")
    done = await adapter.rotate_finish(PUSH, CredentialState(current=None, previous=PREV_SLOT))
    replay.assert_exhausted()
    assert done.change.state is ChangeState.DELETED


# ---- deleting and listing ----------------------------------------------------------------------
async def test_delete_credential_removes_both_slots() -> None:
    """A2: the robots of `current` and `previous` go; the new state has no slot."""
    replay, adapter = build("credentials.json", "delete_both")
    outcome = await adapter.delete_credential(PUSH, state_of(SLOT, PREV_SLOT))
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.DELETED
    assert outcome.state == CredentialState(current=None)


async def test_delete_credential_absent_is_unchanged() -> None:
    """A2: nothing to delete (also: no slots at all)."""
    replay, adapter = build("credentials.json", "delete_absent")
    outcome = await adapter.delete_credential(PUSH, state_of())
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.UNCHANGED
    nothing = await offline_adapter().delete_credential(PUSH, CredentialState(current=None))
    assert nothing.change.state is ChangeState.UNCHANGED


async def test_delete_credential_tolerates_a_vanished_robot() -> None:
    """A 404 on DELETE after the lookup means someone else deleted it: UNCHANGED."""
    replay, adapter = build("credentials.json", "finish_race")
    outcome = await adapter.delete_credential(
        PUSH, CredentialState(current=PREV_SLOT, previous=None)
    )
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.UNCHANGED


async def test_list_credentials_returns_only_this_tenants_robots() -> None:
    """A4: only robots named with the tenant prefix are listed, with project and id in the ref."""
    replay, adapter = build("credentials.json", "list")
    listed = await adapter.list_credentials(TENANT)
    replay.assert_exhausted()
    assert {(item.name, item.ref["robot_id"], item.ref["project"]) for item in listed} == {
        (CRED, "11", PROJECT),
        (PREV, "10", PROJECT),
    }


# ---- github outputs (pure) -------------------------------------------------------------------
def test_github_outputs_for_a_secret() -> None:
    """The registry host is a variable; the robot's name and secret are GitHub secrets."""
    issued = IssuedSecret({"username": SecretStr("robot$p+n"), "password": SecretStr("pw")})
    scope = make_scope("push", env="prod")
    outputs = offline_adapter().github_outputs(scope, issued)
    assert outputs == [
        GitHubOutput("variable", "HARBOR_REGISTRY", "harbor.example", 10, "prod"),
        GitHubOutput("secret", "HARBOR_USERNAME", issued.values["username"], 10, "prod"),
        GitHubOutput("secret", "HARBOR_PASSWORD", issued.values["password"], 10, "prod"),
    ]


def test_github_outputs_without_a_ci_credential_or_with_oidc() -> None:
    """No CI credential: no outputs. OIDC trust is not something Harbor can provide."""
    adapter = offline_adapter()
    assert adapter.github_outputs(PUSH, NoCiCredential("n/a")) == []
    with pytest.raises(AdapterError) as info:
        adapter.github_outputs(PUSH, OidcTrust(GITHUB_ISSUER, "aud", {}))
    assert not info.value.retryable


# ---- health and error mapping (A5) -----------------------------------------------------------
async def test_health_check_reads_the_current_user() -> None:
    """The check authenticates, so a bad credential reports `ok=False`."""
    replay, adapter = build("credentials.json", "health_ok")
    assert (await adapter.health_check()).ok
    replay.assert_exhausted()
    _, adapter = build("credentials.json", "health_unauthorized")
    health = await adapter.health_check()
    assert not health.ok
    assert health.detail is not None


@pytest.mark.parametrize(
    ("scenario", "retryable"),
    [("server_error", True), ("bad_request", False), ("transport_error", True)],
)
async def test_errors_are_classified_and_carry_no_body(scenario: str, retryable: bool) -> None:
    """A5: 5xx and transport errors retry, other statuses do not; nothing secret leaks."""
    _, adapter = build("credentials.json", scenario)
    health = await adapter.health_check()
    assert not health.ok
    detail = health.detail or ""
    assert "secret-in-body" not in detail
    assert ADMIN_SECRET not in detail
    replay = Replay(load_scenario("credentials.json", scenario, CONTEXT))
    async with HarborClient(
        BASE_URL, ADMIN_USER, SecretStr(ADMIN_SECRET), replay.transport()
    ) as client:
        with pytest.raises(AdapterError) as info:
            await client.call("GET", "/api/v2.0/users/current")
    assert info.value.retryable is retryable
    assert "secret-in-body" not in info.value.message
