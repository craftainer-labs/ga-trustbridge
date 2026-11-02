"""Edge paths of the GitHub core: checker, monitor loop, handlers, writer, manifest, reporting."""

import asyncio
import datetime as dt
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from tests.trustbridge.github.conftest import (
    Env,
    FakeChecker,
    KeySigner,
    ScriptedGitHub,
    make_host_config,
)
from tests.trustbridge.github.test_manifest_handlers import PEM, Flow, state_of
from tests.trustbridge.github.test_tokens_writer import script_repo
from trustbridge.adapters.interface import GitHubOutput
from trustbridge.adapters.store import DeliveredOutput, OutputError
from trustbridge.github.app_jwt import verify_rs256
from trustbridge.github.availability import (
    HostMonitor,
    HostNotReadyError,
    HostState,
    HttpHostChecker,
    MonitorDeps,
)
from trustbridge.github.checks import CheckReport, CheckRunReporter
from trustbridge.github.client import ApiResponse, GitHubApi, GitHubApiError
from trustbridge.github.deployments import (
    ArgoCdDeploymentHandler,
    ArgoTrigger,
    DeploymentReport,
    DeploymentReporter,
    DeploymentState,
)
from trustbridge.github.handlers import EventContext, InstallationHandlers
from trustbridge.github.hosts import HostSettingsError, load_hosts, validate_hosts
from trustbridge.github.manifest import AppKind, CompleteRequest, ManifestFlowError, StartRequest
from trustbridge.github.writer import GitHubEnvironmentInspector, GitHubOutputWriter
from trustbridge.signer.config import FileSignerConfig
from trustbridge.worker.ports import MemoryTenants, ReconcileRequests


def _ca_bundle(tmp_path: Path) -> Path:
    """A self-signed certificate on disk, usable as a CA bundle."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    path = tmp_path / "ca.pem"
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return path


class StubApi:
    """A requester answering `GET /app` with a fixed id."""

    async def request_as_app(self, method: str, path: str, **_kw: Any) -> ApiResponse:  # noqa: ANN401
        """Answer the App id."""
        assert (method, path) == ("GET", "/app")
        return ApiResponse(200, {"id": 77})


async def test_the_http_checker_reads_the_app_id_and_the_discovery_issuer(tmp_path: Path) -> None:
    """`GET /app` returns the id; discovery honours the host CA bundle."""

    def discovery(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.well-known/openid-configuration"
        return httpx.Response(200, json={"issuer": "https://token.actions.githubusercontent.com"})

    checker = HttpHostChecker(
        api_factory=lambda _host: StubApi(),  # type: ignore[arg-type,return-value]
        discovery_transport=httpx.MockTransport(discovery),
    )
    host = make_host_config()
    assert await checker.get_app_id(host, "jwt") == 77
    assert await checker.discovery_issuer(host) == host.oidc_issuer
    with_ca = host.model_copy(update={"ca_bundle": _ca_bundle(tmp_path)})
    assert await checker.discovery_issuer(with_ca) == host.oidc_issuer


async def test_a_host_uses_its_ca_bundle_for_api_calls(tmp_path: Path) -> None:
    """The githubkit client is built with the host's CA bundle."""
    gh = ScriptedGitHub()
    gh.route("GET", "/app", body={"id": 1})
    host = make_host_config().model_copy(update={"ca_bundle": _ca_bundle(tmp_path)})
    api = GitHubApi(host, async_transport=gh.transport())
    assert (await api.request_as_app("GET", "/app", jwt="j")).data == {"id": 1}


async def test_a_host_whose_signer_is_missing_is_unavailable() -> None:
    """The signer name in the host entry must exist in the signer map."""
    mon = HostMonitor([make_host_config()], {}, MonitorDeps(checker=FakeChecker()))
    await mon.check_all()
    assert mon.status("github.com").reason == "signer 'main' is not available"


async def test_the_recheck_task_starts_once_stops_cleanly_and_survives_a_failed_round() -> None:
    """`start` twice keeps one task, `stop` without a task is fine, a failing round is logged."""
    mon = HostMonitor(
        [make_host_config()], {"main": KeySigner()}, MonitorDeps(checker=FakeChecker())
    )
    await mon.stop()  # nothing started yet
    rounds: list[int] = []

    async def failing_round() -> None:
        rounds.append(1)
        raise RuntimeError("boom")

    mon.recheck = failing_round  # type: ignore[method-assign]
    mon.start(0.001)
    first = mon._task
    mon.start(0.001)
    assert mon._task is first
    while len(rounds) < 2:
        await asyncio.sleep(0.005)  # the loop survives a failing round
    await mon.stop()
    assert mon._task is None


async def test_check_run_conclusion_is_only_sent_for_a_completed_run(env: Env) -> None:
    """An in-progress report omits the conclusion."""
    script_repo(env)
    env.gh.route("GET", "/repos/acme/web/commits/abc/check-runs", body={"check_runs": []})
    env.gh.route("POST", "/repos/acme/web/check-runs", 201, {"id": 5})
    report = CheckReport(
        repository_id=10,
        head_sha="abc",
        name="n",
        status="in_progress",
        conclusion=None,
        title="t",
        summary="s",
    )
    assert await CheckRunReporter(env.gateway).report(env.tenant, report) == 5
    assert "conclusion" not in env.gh.json_of("POST", "/repos/acme/web/check-runs")


async def test_deployment_status_carries_the_environment_url(env: Env) -> None:
    """`environment_url` is sent when the report has one."""
    script_repo(env)
    env.gh.route("GET", "/repos/acme/web/deployments", body=[{"id": 9}])
    env.gh.route("GET", "/repos/acme/web/deployments/9/statuses", body=[])
    env.gh.route("POST", "/repos/acme/web/deployments/9/statuses", 201, {})
    await DeploymentReporter(env.gateway).report(
        env.tenant,
        DeploymentReport(10, "abc", "prod", DeploymentState.SUCCESS, environment_url="https://e"),
    )
    body = env.gh.json_of("POST", "/repos/acme/web/deployments/9/statuses")
    assert body["environment_url"] == "https://e"


async def test_the_argocd_handler_ignores_unknown_applications(env: Env) -> None:
    """No trigger and an unknown application both report nothing."""

    class Nothing:
        async def observe(self, tenant: Any, trigger: ArgoTrigger) -> None:  # noqa: ANN401
            """Know nothing."""
            del tenant, trigger

    handler = ArgoCdDeploymentHandler(Nothing(), DeploymentReporter(env.gateway))
    assert await handler.handle(env.tenant, {}) is None
    assert await handler.handle(env.tenant, {"app": "web"}) is None


async def test_the_gateway_forgets_a_renamed_repository(env: Env) -> None:
    """A rename drops the cached full name, so the next call asks GitHub again."""
    script_repo(env)
    assert await env.gateway.full_name(env.tenant, 10) == "acme/web"
    env.gh.route("GET", "/repositories/10", body={"full_name": "acme/web2"})
    env.gateway.forget_repository(env.tenant, 10)
    assert await env.gateway.full_name(env.tenant, 10) == "acme/web2"


Wired = tuple[InstallationHandlers, MemoryTenants, ReconcileRequests, list[tuple[str, int]]]


def _handlers(with_callback: bool) -> Wired:
    tenants = MemoryTenants(lambda _name: make_host_config().to_host())
    scheduler = ReconcileRequests()
    changed: list[tuple[str, int]] = []
    callback = (lambda host, inst: changed.append((host, inst))) if with_callback else None
    return InstallationHandlers(tenants, scheduler, callback), tenants, scheduler, changed


def _ctx(event: str, action: str | None, payload: dict[str, Any]) -> EventContext:
    return EventContext("github.com", "d1", event, action, payload)


INSTALLATION = {"id": 7, "account": {"login": "acme", "id": 1}}


async def test_installation_events_work_without_a_token_cache_callback() -> None:
    """The callback is optional; an unknown tenant is not scheduled."""
    handlers, tenants, scheduler, _ = _handlers(with_callback=False)
    await handlers.on_installation(_ctx("installation", "suspend", {"installation": INSTALLATION}))
    await handlers.on_installation(
        _ctx("installation", "new_permissions_accepted", {"installation": INSTALLATION})
    )
    assert scheduler.local == set()  # nothing known yet
    await handlers.on_installation(_ctx("installation", "created", {"installation": INSTALLATION}))
    await handlers.on_installation(
        _ctx("installation", "new_permissions_accepted", {"installation": INSTALLATION})
    )
    tenant_id = await tenants.tenant_id_for("github.com", 7)
    assert scheduler.local == {str(tenant_id)}


async def test_installation_events_notify_the_callback_and_ignore_other_actions() -> None:
    """Suspend and permission changes drop cached tokens; an unlisted action does nothing."""
    handlers, _, scheduler, changed = _handlers(with_callback=True)
    await handlers.on_installation(_ctx("installation", "suspend", {"installation": INSTALLATION}))
    await handlers.on_installation(
        _ctx("installation", "new_permissions_accepted", {"installation": INSTALLATION})
    )
    await handlers.on_installation(_ctx("installation", "weird", {"installation": INSTALLATION}))
    assert changed == [("github.com", 7), ("github.com", 7)]
    assert scheduler.local == set()


async def test_repository_events_cover_unknown_tenants_and_metadata_changes() -> None:
    """No installation or an unknown tenant is ignored; metadata edits schedule a reconcile."""
    handlers, tenants, scheduler, _ = _handlers(with_callback=False)
    repo = {"id": 5, "full_name": "acme/web"}
    await handlers.on_repository(_ctx("repository", "renamed", {"repository": repo}))
    await handlers.on_repository(
        _ctx("repository", "renamed", {"repository": repo, "installation": {"id": 7}})
    )
    assert scheduler.local == set()
    tenant_id = await tenants.upsert_installation("github.com", 7, "acme", 1)
    payload = {"repository": repo, "installation": {"id": 7}}
    await handlers.on_repository(_ctx("repository", "archived", payload))
    assert scheduler.local == {str(tenant_id)}
    scheduler.local.clear()
    await handlers.on_repository(_ctx("repository", "created", payload))
    assert scheduler.local == set()  # an action the core does not track


def test_host_settings_problems_cover_missing_hosts_and_ghe_com_paths() -> None:
    """A URL without a host, and a ghe.com API URL with a path, are settings errors."""
    nohost = load_hosts([
        {
            "name": "h",
            "kind": "github.com",
            "web_url": "https://",
            "api_url": "https://api.github.com",
            "oidc_issuer": "https://token.actions.githubusercontent.com",
            "registry_host": "ghcr.io",
            "signer": "s",
        }
    ])
    with pytest.raises(HostSettingsError, match="has no host"):
        validate_hosts(nohost, {"s": _file_signer()}, production=False)
    ghe = load_hosts([
        {
            "name": "e",
            "kind": "ghe.com",
            "web_url": "https://acme.ghe.com",
            "api_url": "https://api.acme.ghe.com/v3",
            "oidc_issuer": "https://x",
            "registry_host": "r",
            "signer": "s",
        }
    ])
    with pytest.raises(HostSettingsError, match="no path"):
        validate_hosts(ghe, {"s": _file_signer()}, production=False)


def _file_signer() -> FileSignerConfig:
    return FileSignerConfig(kind="file", path=Path("/p.pem"))


async def test_writer_wraps_unready_hosts_and_github_failures(env: Env) -> None:
    """Writes and deletes against an unavailable host or a failing GitHub are output errors."""
    script_repo(env)
    writer = GitHubOutputWriter(env.gateway)
    env.gh.route("DELETE", "/repos/acme/web/actions/secrets/A", 500, {"message": "x"})
    with pytest.raises(OutputError) as failed:
        await writer.remove(env.tenant, [DeliveredOutput("secret", "A", 10, None)])
    assert failed.value.retryable is True
    env.checker.issuer = "https://evil.example"
    await env.monitor.check_all()  # the host is unavailable now
    with pytest.raises(OutputError, match="unavailable"):
        await writer.remove(env.tenant, [DeliveredOutput("secret", "A", 10, None)])
    with pytest.raises(OutputError, match="unavailable"):
        await writer.apply(env.tenant, [GitHubOutput("variable", "V", "1", 10, None)])


async def test_writer_environment_scoping_and_error_propagation(env: Env) -> None:
    """Org outputs cannot target an environment; non-404 variable and delete errors surface."""
    script_repo(env)
    writer = GitHubOutputWriter(env.gateway)
    with pytest.raises(OutputError, match="needs a repository") as perm:
        await writer.apply(env.tenant, [GitHubOutput("secret", "A", "v", None, "prod")])
    assert perm.value.retryable is False
    env.gh.route("PATCH", "/repos/acme/web/actions/variables/V", 500, {"message": "x"})
    with pytest.raises(OutputError):
        await writer.apply(env.tenant, [GitHubOutput("variable", "V", "1", 10, None)])
    env.gh.route("PATCH", "/orgs/acme/actions/variables/OV", 404, {})
    env.gh.route("POST", "/orgs/acme/actions/variables", 201, {})
    await writer.apply(env.tenant, [GitHubOutput("variable", "OV", "1", None, None)])
    assert env.gh.json_of("POST", "/orgs/acme/actions/variables")["visibility"] == "private"
    env.gh.route("DELETE", "/repos/acme/web/actions/variables/V", 403, {})
    with pytest.raises(OutputError):
        await writer.remove(env.tenant, [DeliveredOutput("variable", "V", 10, None)])


async def test_the_inspector_surfaces_unexpected_github_errors(env: Env) -> None:
    """Only a missing environment is "not protected"; other failures propagate."""
    script_repo(env)
    env.gh.route("GET", "/repos/acme/web/environments/prod", 500, {})
    with pytest.raises(GitHubApiError):
        await GitHubEnvironmentInspector(env.gateway).is_protected(env.tenant, 10, "prod")


async def test_manifest_flow_rejects_malformed_state_and_missing_webhook_secret(
    tmp_path: Path,
) -> None:
    """A garbled state is a flow error; GitHub must return a webhook secret for the main App."""
    f = Flow(tmp_path)
    with pytest.raises(ManifestFlowError, match="malformed state"):
        await f.flow.complete(f.host, CompleteRequest("c0de", "x.y", "s"))
    f.gh.route(
        "POST",
        "/app-manifests/c0de/conversions",
        201,
        {
            "id": 123,
            "slug": "tb",
            "html_url": "https://github.com/apps/tb",
            "pem": PEM,
        },
    )
    started = await f.flow.start(f.host, StartRequest(AppKind.TRUSTBRIDGE, "s"))
    with pytest.raises(ManifestFlowError, match="no webhook secret"):
        await f.flow.complete(f.host, CompleteRequest("c0de", state_of(started.post_url), "s"))


def test_a_non_rsa_public_key_never_verifies() -> None:
    """An EC key is not an acceptable App key."""
    ec_pub = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    assert verify_rs256(ec_pub, b"x", b"y") is False


async def test_an_unready_host_raises_for_gateway_users() -> None:
    """Sanity: `require_ready` is the single gate."""
    mon = HostMonitor(
        [make_host_config()], {"main": KeySigner()}, MonitorDeps(checker=FakeChecker())
    )
    with pytest.raises(HostNotReadyError):
        mon.require_ready("github.com")
    assert mon.status("github.com").state is HostState.UNAVAILABLE
