"""Contract tests for the Docker Hub adapter: it replays recorded-style API exchanges (no network).

Each test names the ADR 0029 invariant it covers. The fixtures under `fixtures/` are built from
Docker's documented request and response shapes; the module README lists what is unconfirmed.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from tests.trustbridge.adapters.dockerhub.replay import BEARER, PAT, Replay, load_scenario
from trustbridge.adapters.dockerhub import DockerHubAdapter
from trustbridge.adapters.dockerhub import adapter as adapter_module
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
)
from trustbridge.adapters.loader import instantiate, validate_capabilities

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")
HOST = GitHubHost(
    name="github.com",
    kind="github.com",
    api_url="https://api.github.com",
    oidc_issuer="https://token.actions.githubusercontent.com",
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


PULL = make_scope("pull")  # project-wide grant
PUSH = make_scope("push", env="prod", platform_repository_key="api")  # per-repository grant
ADMIN = make_scope("admin", env="prod", platform_repository_key="web")
CONTEXT = {
    "expires": "2027-01-30T12:00:00Z",
    "cred_pull": credential_name(TENANT_ID, PULL.key, 1),
    "cred_push": credential_name(TENANT_ID, PUSH.key, 2),
    "prev": credential_name(TENANT_ID, PUSH.key, 1),
    "cred_admin": credential_name(TENANT_ID, ADMIN.key, 1),
}
PULL_SLOT = CredentialSlot(1, CONTEXT["cred_pull"])
DELIVERED_PULL = CredentialState(replace(PULL_SLOT, delivered=True))
PUSH_SLOT = CredentialSlot(2, CONTEXT["cred_push"])
PREV_SLOT = CredentialSlot(1, CONTEXT["prev"], {"kind": "oat", "token_id": "t-prev"}, True)
PROJECT_REF = ProjectRef(TENANT, ResourceState("acme", {"kind": "project", "namespace": "acme"}))


def config(overrides: dict[str, SecretStr | None] | None = None) -> AdapterConfig:
    """Build the tenant's connection data."""
    connection = {"namespace": SecretStr("acme"), "username": SecretStr("svc-tb")}
    for key, value in (overrides or {}).items():
        if value is None:
            connection.pop(key, None)
        else:
            connection[key] = value
    return AdapterConfig(TENANT, connection, SecretStr(PAT))


def build(file: str, scenario: str) -> tuple[Replay, DockerHubAdapter]:
    """Return a replay of the scenario and an adapter wired to it."""
    replay = Replay(load_scenario(file, scenario, CONTEXT))
    adapter = DockerHubAdapter(config(), transport=replay.transport(), clock=lambda: NOW)
    return replay, adapter


def offline_adapter(**overrides: SecretStr | None) -> DockerHubAdapter:
    """An adapter whose transport must never be used."""
    return DockerHubAdapter(config(overrides), transport=Replay([]).transport(), clock=lambda: NOW)


# ---- capabilities and loading (A7) -----------------------------------------------------------
def test_capabilities_match_the_adr_matrix() -> None:
    """ADR 0029 matrix: no OIDC, OATs, two-phase, 10-token cap, unauthenticated trigger hooks."""
    adapter = offline_adapter()
    caps = adapter.capabilities()
    validate_capabilities(caps)
    assert not caps.oidc
    assert caps.oidc_hosts == frozenset()
    assert not caps.oidc_requires_manual_step
    assert caps.ci_credential
    assert caps.rotation == "two_phase"
    assert caps.credential_limit == 10
    assert caps.needs_privileged_credential
    assert caps.webhook is not None
    assert caps.webhook.auth is WebhookAuth.NONE
    assert caps.webhook.trigger_only
    assert caps.github_hosts == frozenset({"github.com", "ghes", "ghe.com"})
    assert isinstance(adapter, PlatformAdapter)


def test_the_loader_instantiates_with_the_config_only() -> None:
    """The loader calls `cls(config)`."""
    assert isinstance(instantiate(DockerHubAdapter, config()), DockerHubAdapter)


# ---- configuration errors --------------------------------------------------------------------
async def test_missing_pat_is_a_configuration_error() -> None:
    """No privileged credential: a non-retryable configuration error, health reports it."""
    adapter = DockerHubAdapter(
        AdapterConfig(TENANT, {"namespace": SecretStr("acme"), "username": SecretStr("u")}, None),
        transport=Replay([]).transport(),
    )
    with pytest.raises(AdapterConfigurationError):
        await adapter.ensure_project(TENANT, ProjectSpec("default"), None)
    health = await adapter.health_check()
    assert not health.ok
    assert health.detail is not None


@pytest.mark.parametrize("missing", ["namespace", "username"])
async def test_missing_connection_data_is_a_configuration_error(missing: str) -> None:
    """A namespace and a username are required connection data."""
    adapter = offline_adapter(**{missing: None})
    with pytest.raises(AdapterConfigurationError):
        await adapter.ensure_project(TENANT, ProjectSpec("default"), None)
    with pytest.raises(AdapterConfigurationError):
        await adapter.list_credentials(TENANT)


@pytest.mark.parametrize("missing", ["namespace", "username"])
async def test_empty_connection_data_is_a_configuration_error(missing: str) -> None:
    """An empty namespace or username is as bad as a missing one."""
    adapter = offline_adapter(**{missing: SecretStr("")})
    with pytest.raises(AdapterConfigurationError):
        await adapter.ensure_project(TENANT, ProjectSpec("default"), None)


@pytest.mark.parametrize(
    "url", ["http://hub.docker.com", "https://", "https://h.example/?q=1", "https://h.example/#f"]
)
async def test_base_url_must_be_plain_https(url: str) -> None:
    """The operator-provisioned base_url must be https without query or fragment."""
    adapter = offline_adapter(base_url=SecretStr(url))
    with pytest.raises(AdapterConfigurationError):
        await adapter.ensure_project(TENANT, ProjectSpec("default"), None)


async def test_base_url_override_is_used_and_trailing_slash_dropped() -> None:
    """An operator base_url replaces the default host."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/v2/auth/token":
            return httpx.Response(200, json={"access_token": BEARER})
        return httpx.Response(200, json={"results": []})

    adapter = DockerHubAdapter(
        config({"base_url": SecretStr("https://hub.example.com/")}),
        transport=httpx.MockTransport(handler),
    )
    health = await adapter.health_check()
    assert health.ok
    assert seen[0] == "https://hub.example.com/v2/auth/token"
    assert seen[1].startswith("https://hub.example.com/v2/namespaces/acme/repositories")


# ---- projects (the namespace) ----------------------------------------------------------------
async def test_ensure_project_confirms_the_namespace() -> None:
    """The namespace is the project; nothing is created, so the answer is UNCHANGED."""
    replay, adapter = build("projects.json", "namespace_ok")
    applied = await adapter.ensure_project(TENANT, ProjectSpec("default"), None)
    replay.assert_exhausted()
    assert applied.change.state is ChangeState.UNCHANGED
    assert applied.state == ResourceState("acme", {"kind": "project", "namespace": "acme"})


async def test_ensure_project_fails_for_an_unknown_namespace() -> None:
    """A namespace that does not exist is a non-retryable error."""
    replay, adapter = build("projects.json", "namespace_missing")
    with pytest.raises(AdapterError) as err:
        await adapter.ensure_project(TENANT, ProjectSpec("default"), None)
    replay.assert_exhausted()
    assert not err.value.retryable


# ---- repositories ----------------------------------------------------------------------------
async def test_ensure_repository_creates_a_private_marked_repository() -> None:
    """A2: GET 404 then POST creates a private repository carrying the ownership marker."""
    replay, adapter = build("repositories.json", "repo_create")
    applied = await adapter.ensure_repository(PROJECT_REF, RepoSpec("api"), None)
    replay.assert_exhausted()
    assert applied.change.state is ChangeState.CREATED
    assert applied.state == ResourceState(
        "api", {"kind": "repository", "namespace": "acme", "repository": "api"}
    )


async def test_ensure_repository_is_idempotent() -> None:
    """A3: a repository with the marker is UNCHANGED."""
    replay, adapter = build("repositories.json", "repo_exists_managed")
    applied = await adapter.ensure_repository(PROJECT_REF, RepoSpec("api"), ResourceState("x"))
    replay.assert_exhausted()
    assert applied.change.state is ChangeState.UNCHANGED


async def test_ensure_repository_never_adopts_a_foreign_repository() -> None:
    """A repository that exists without the marker is refused (non-retryable)."""
    replay, adapter = build("repositories.json", "repo_foreign")
    with pytest.raises(AdapterError, match="not managed") as err:
        await adapter.ensure_repository(PROJECT_REF, RepoSpec("api"), None)
    replay.assert_exhausted()
    assert not err.value.retryable


async def test_ensure_repository_converges_after_a_lost_race() -> None:
    """A 409 on POST re-reads the repository and, with the marker, reports UNCHANGED."""
    replay, adapter = build("repositories.json", "repo_race")
    applied = await adapter.ensure_repository(PROJECT_REF, RepoSpec("api"), None)
    replay.assert_exhausted()
    assert applied.change.state is ChangeState.UNCHANGED


async def test_ensure_repository_rejects_other_package_types_and_bad_names() -> None:
    """Only docker repositories with Docker Hub-valid names are created."""
    adapter = offline_adapter()
    with pytest.raises(AdapterConfigurationError):
        await adapter.ensure_repository(PROJECT_REF, RepoSpec("api", package_type="maven"), None)
    for bad in ("A", "a", "api/../x", "Api", "-api", "x" * 256):
        with pytest.raises(AdapterConfigurationError):
            await adapter.ensure_repository(PROJECT_REF, RepoSpec(bad), None)


# ---- webhooks and deleting resources ---------------------------------------------------------
async def test_ensure_webhook_reports_a_manual_step() -> None:
    """No webhook API is documented: the adapter creates nothing and says what to do."""
    target = WebhookTarget(TENANT, "https://tb.example/hooks/dockerhub", ("push",), "ref")
    applied = await offline_adapter().ensure_webhook(target, None)
    assert applied.change.state is ChangeState.NEEDS_MANUAL_ACTION
    assert applied.change.manual_action is not None
    assert "Webhooks tab" in applied.change.manual_action
    assert applied.state is None


async def test_delete_never_deletes_repositories_or_namespaces() -> None:
    """A repository is left for a person; a namespace or webhook has nothing to delete."""
    adapter = offline_adapter()
    repo = await adapter.delete(ResourceState("api", {"kind": "repository"}))
    assert repo.change.state is ChangeState.NEEDS_MANUAL_ACTION
    assert repo.change.manual_action is not None
    assert repo.state is None
    for kind in ("project", "webhook"):
        applied = await adapter.delete(ResourceState("x", {"kind": kind}))
        assert applied.change.state is ChangeState.UNCHANGED
    with pytest.raises(AdapterError, match="unknown resource kind"):
        await adapter.delete(ResourceState("x"))


# ---- issuing credentials ---------------------------------------------------------------------
async def test_ensure_credential_project_wide_grant() -> None:
    """platform_repository_key unset: the OAT covers every repository of the namespace."""
    replay, adapter = build("credentials.json", "issue_project_wide")
    outcome = await adapter.ensure_credential(PULL, CredentialState(PULL_SLOT))
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.CREATED
    assert isinstance(outcome.credential, IssuedSecret)
    assert outcome.credential.values["token"].get_secret_value() == "dckr_oat_dummy_t1"
    assert outcome.credential.expires_at == datetime(2027, 1, 30, 12, 0, tzinfo=UTC)
    current = outcome.state.current
    assert current is not None
    assert current.ref == {"kind": "oat", "token_id": "t1"}
    assert not current.delivered
    body = json.loads(replay.seen[-1].content)
    assert body["resources"] == [
        {"type": "TYPE_REPO", "path": "acme/*", "scopes": ["scope-image-pull"]}
    ]
    assert PAT not in repr(outcome)


async def test_ensure_credential_per_repository_grant() -> None:
    """platform_repository_key set: the OAT is limited to that one repository (push scope)."""
    replay, adapter = build("credentials.json", "issue_per_repo")
    outcome = await adapter.ensure_credential(PUSH, CredentialState(PUSH_SLOT))
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.CREATED
    body = json.loads(replay.seen[-1].content)
    assert body["resources"] == [
        {"type": "TYPE_REPO", "path": "acme/api", "scopes": ["scope-image-push"]}
    ]


async def test_ensure_credential_admin_permission_never_gets_repository_admin() -> None:
    """admin = delete images + edit the repository; never repository-admin or repository-create."""
    replay, adapter = build("credentials.json", "issue_admin")
    outcome = await adapter.ensure_credential(
        ADMIN, CredentialState(CredentialSlot(1, CONTEXT["cred_admin"]))
    )
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.CREATED
    scopes = json.loads(replay.seen[-1].content)["resources"][0]["scopes"]
    assert scopes == ["scope-image-delete", "scope-repository-edit"]


async def test_ensure_credential_rejects_a_glob_in_the_repository_key() -> None:
    """A repository key can never widen the grant (`*` or a path)."""
    adapter = offline_adapter()
    for bad in ("*", "a/b", ""):
        scope = replace(PUSH, platform_repository_key=bad)
        with pytest.raises(AdapterConfigurationError):
            adapter._resources(scope)


async def test_ensure_credential_is_idempotent_when_delivered() -> None:
    """A3: a delivered, usable OAT with the right grant is UNCHANGED and returns no secret."""
    replay, adapter = build("credentials.json", "confirm_unchanged")
    outcome = await adapter.ensure_credential(PULL, DELIVERED_PULL)
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.UNCHANGED
    assert outcome.credential is None
    assert outcome.state == DELIVERED_PULL


@pytest.mark.parametrize("scenario", ["confirm_drift", "confirm_no_resources"])
async def test_ensure_credential_regrants_on_drift(scenario: str) -> None:
    """A drifted (or unreadable) resource grant is PATCHed back; the secret is not re-issued."""
    replay, adapter = build("credentials.json", scenario)
    outcome = await adapter.ensure_credential(PULL, DELIVERED_PULL)
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.UPDATED
    assert outcome.credential is None


@pytest.mark.parametrize("scenario", ["reissue_inactive", "reissue_expired"])
async def test_ensure_credential_reissues_an_unusable_token(scenario: str) -> None:
    """A deactivated or expired OAT is deleted (it counts toward the limit) and replaced."""
    replay, adapter = build("credentials.json", scenario)
    outcome = await adapter.ensure_credential(PULL, DELIVERED_PULL)
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.UPDATED
    assert isinstance(outcome.credential, IssuedSecret)


async def test_ensure_credential_replaces_an_undelivered_token_whose_secret_is_lost() -> None:
    """A2: a crash after creating the OAT leaves one under the label; delete it, create again."""
    replay, adapter = build("credentials.json", "reissue_undelivered")
    outcome = await adapter.ensure_credential(PULL, CredentialState(PULL_SLOT))
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.UPDATED
    assert isinstance(outcome.credential, IssuedSecret)


async def test_ensure_credential_tolerates_a_token_deleted_meanwhile() -> None:
    """A 404 on the DELETE of a stale OAT (someone else removed it) does not stop the re-issue."""
    replay, adapter = build("credentials.json", "reissue_already_gone")
    outcome = await adapter.ensure_credential(PULL, CredentialState(PULL_SLOT))
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.UPDATED


async def test_ensure_credential_without_a_current_slot_fails() -> None:
    """The core always names the slot; without one the call is a non-retryable error."""
    with pytest.raises(AdapterError, match="no current slot"):
        await offline_adapter().ensure_credential(PULL, CredentialState(None))


async def test_a_foreign_credential_name_is_never_touched() -> None:
    """Ownership marker: names without this tenant's prefix are refused, never deleted."""
    adapter = offline_adapter()
    foreign = CredentialState(CredentialSlot(1, "tb-ffffffffffffffffffffffffffffffff-x-g1"))
    with pytest.raises(AdapterError, match="not a trustbridge name"):
        await adapter.ensure_credential(PULL, foreign)
    with pytest.raises(AdapterError, match="not a trustbridge name"):
        await adapter.delete_credential(PULL, foreign)
    with pytest.raises(AdapterError, match="not a trustbridge name"):
        await adapter.rotate_finish(PULL, CredentialState(PUSH_SLOT, foreign.current))


@pytest.mark.parametrize(
    ("scenario", "retryable"), [("issue_limit_reached", False), ("issue_rate_limited", True)]
)
async def test_issue_errors_are_classified(scenario: str, retryable: bool) -> None:
    """A4: 403 (token limit) ends the operation; 429 is retried. No body reaches the message."""
    replay, adapter = build("credentials.json", scenario)
    with pytest.raises(AdapterError) as err:
        await adapter.ensure_credential(PULL, CredentialState(PULL_SLOT))
    replay.assert_exhausted()
    assert err.value.retryable is retryable
    assert "token limit" not in err.value.message
    assert "slow down" not in err.value.message


async def test_issue_rejects_an_unexpected_response() -> None:
    """A creation response without the token or id is a non-retryable error."""
    replay, adapter = build("credentials.json", "issue_bad_response")
    with pytest.raises(AdapterError, match="unexpected token response"):
        await adapter.ensure_credential(PULL, CredentialState(PULL_SLOT))
    replay.assert_exhausted()


# ---- rotation (two-phase) --------------------------------------------------------------------
async def test_rotate_begin_creates_the_next_generation_and_keeps_previous() -> None:
    """A8: the new OAT is created under the next label; `previous` stays untouched and valid."""
    replay, adapter = build("credentials.json", "rotate_begin")
    prior = CredentialState(PUSH_SLOT, PREV_SLOT)
    outcome = await adapter.rotate_begin(PUSH, prior)
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.CREATED
    assert isinstance(outcome.credential, IssuedSecret)
    assert outcome.state.previous == PREV_SLOT
    assert outcome.state.current is not None
    assert outcome.state.current.name == CONTEXT["cred_push"]
    assert outcome.state.current.ref == {"kind": "oat", "token_id": "t5"}


async def test_rotate_begin_resumes_after_a_crash() -> None:
    """A retried rotate_begin deletes the half-made OAT of the same label, never previous's."""
    replay, adapter = build("credentials.json", "rotate_begin_retry")
    outcome = await adapter.rotate_begin(PUSH, CredentialState(PUSH_SLOT, PREV_SLOT))
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.UPDATED
    assert outcome.state.previous == PREV_SLOT


async def test_rotate_finish_deletes_previous_only() -> None:
    """A8: rotate_finish removes the previous generation's OAT and keeps current."""
    replay, adapter = build("credentials.json", "rotate_finish")
    prior = CredentialState(replace(PUSH_SLOT, delivered=True), PREV_SLOT)
    outcome = await adapter.rotate_finish(PUSH, prior)
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.DELETED
    assert outcome.state == CredentialState(current=prior.current)


async def test_rotate_finish_is_idempotent() -> None:
    """Absent previous token, or no previous slot: UNCHANGED."""
    replay, adapter = build("credentials.json", "rotate_finish_absent")
    gone = await adapter.rotate_finish(PUSH, CredentialState(PUSH_SLOT, PREV_SLOT))
    replay.assert_exhausted()
    assert gone.change.state is ChangeState.UNCHANGED
    none = await offline_adapter().rotate_finish(PUSH, CredentialState(PUSH_SLOT))
    assert none.change.state is ChangeState.UNCHANGED


# ---- deleting credentials --------------------------------------------------------------------
async def test_delete_credential_removes_current_and_previous() -> None:
    """Both generations' OATs are deleted by label."""
    replay, adapter = build("credentials.json", "delete_both")
    outcome = await adapter.delete_credential(PUSH, CredentialState(PUSH_SLOT, PREV_SLOT))
    replay.assert_exhausted()
    assert outcome.change.state is ChangeState.DELETED
    assert outcome.state == CredentialState(current=None)


async def test_delete_credential_is_idempotent() -> None:
    """A 404 on DELETE, an empty listing, or no slots at all is UNCHANGED."""
    replay, adapter = build("credentials.json", "delete_absent")
    raced = await adapter.delete_credential(PUSH, CredentialState(PUSH_SLOT))
    replay.assert_exhausted()
    assert raced.change.state is ChangeState.UNCHANGED
    replay, adapter = build("credentials.json", "delete_none_listed")
    empty = await adapter.delete_credential(PUSH, CredentialState(PUSH_SLOT))
    replay.assert_exhausted()
    assert empty.change.state is ChangeState.UNCHANGED
    nothing = await offline_adapter().delete_credential(PUSH, CredentialState(None))
    assert nothing.change.state is ChangeState.UNCHANGED


# ---- listing (orphan sweep) ------------------------------------------------------------------
async def test_list_credentials_pages_and_keeps_only_this_tenants_prefix() -> None:
    """Pagination follows `next`; other tenants' and human-made tokens are not listed."""
    replay, adapter = build("credentials.json", "list_two_pages")
    listed = await adapter.list_credentials(TENANT)
    replay.assert_exhausted()
    assert [item.name for item in listed] == [CONTEXT["cred_pull"], CONTEXT["cred_push"]]
    assert listed[0].ref == {"kind": "oat", "token_id": "t1"}


async def test_list_credentials_stops_at_the_page_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A listing that never ends is a non-retryable error, not an endless loop."""
    monkeypatch.setattr(adapter_module, "MAX_PAGES", 1)
    replay, adapter = build("credentials.json", "list_never_ends")
    with pytest.raises(AdapterError, match="too many pages"):
        await adapter.list_credentials(TENANT)
    replay.assert_exhausted()


async def test_list_credentials_rejects_malformed_listings() -> None:
    """A non-list `results`, or a selected entry without an id, is a non-retryable error."""
    replay, adapter = build("credentials.json", "list_not_a_list")
    with pytest.raises(AdapterError, match="not a list"):
        await adapter.list_credentials(TENANT)
    replay.assert_exhausted()
    replay, adapter = build("credentials.json", "list_entry_without_id")
    with pytest.raises(AdapterError, match="without id"):
        await adapter.list_credentials(TENANT)
    replay.assert_exhausted()


# ---- token usability -------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("token", "usable"),
    [
        ({}, True),
        ({"is_active": False}, False),
        ({"expires_at": None}, True),
        ({"expires_at": "2026-10-01T00:00:00Z"}, False),
        ({"expires_at": "2026-12-01T00:00:00Z"}, True),
        ({"expires_at": "2026-12-01T00:00:00"}, True),
        ({"expires_at": "2026-10-01T00:00:00"}, False),
        ({"expires_at": "not a time"}, True),
    ],
)
def test_token_usability(token: dict[str, object], *, usable: bool) -> None:
    """Deactivated and expired OATs are unusable; unreadable expiry is not held against it."""
    assert offline_adapter()._usable(token) is usable


# ---- GitHub outputs --------------------------------------------------------------------------
def test_github_outputs_are_the_login_pair() -> None:
    """The org name as a variable and the OAT as a secret, at the scope's repository and env."""
    secret = IssuedSecret({"token": SecretStr("oat-value")})
    outputs = offline_adapter().github_outputs(PUSH, secret)
    assert [(o.kind, o.name) for o in outputs] == [
        ("variable", "DOCKERHUB_USERNAME"),
        ("secret", "DOCKERHUB_TOKEN"),
    ]
    assert outputs[0].value == "acme"
    assert isinstance(outputs[1].value, SecretStr)
    assert outputs[1].value.get_secret_value() == "oat-value"
    assert all(o.repository_id == 10 and o.environment == "prod" for o in outputs)


def test_github_outputs_are_empty_without_a_secret() -> None:
    """No CI credential, or an OIDC trust (never produced here): nothing is written."""
    adapter = offline_adapter()
    assert adapter.github_outputs(PUSH, NoCiCredential("none")) == []
    assert adapter.github_outputs(PUSH, OidcTrust("https://i", None, {})) == []


# ---- health and authentication ---------------------------------------------------------------
async def test_health_check_logs_in_and_lists() -> None:
    """The PAT is exchanged for a bearer, which the listing then carries."""
    replay, adapter = build("auth.json", "health_ok")
    assert (await adapter.health_check()).ok
    replay.assert_exhausted()


async def test_a_rejected_login_is_non_retryable_and_leaks_nothing() -> None:
    """A5: a 401 from the login ends the operation; neither the PAT nor the body is reported."""
    replay, adapter = build("auth.json", "health_unauthorized")
    health = await adapter.health_check()
    replay.assert_exhausted()
    assert not health.ok
    assert health.detail is not None
    assert PAT not in health.detail
    assert "Incorrect" not in health.detail
    replay, adapter = build("auth.json", "health_unauthorized")
    with pytest.raises(AdapterError) as err:
        await adapter.list_credentials(TENANT)
    assert not err.value.retryable


@pytest.mark.parametrize(
    ("scenario", "retryable"),
    [
        ("login_server_error", True),
        ("login_transport_error", True),
        ("login_no_token", False),
        ("login_not_json", False),
    ],
)
async def test_login_failures_are_classified(scenario: str, retryable: bool) -> None:
    """5xx and transport errors retry; a login answer without a bearer does not."""
    replay, adapter = build("auth.json", scenario)
    with pytest.raises(AdapterError) as err:
        await adapter.list_credentials(TENANT)
    replay.assert_exhausted()
    assert err.value.retryable is retryable
    assert PAT not in err.value.message
