"""Check runs, deployments and statuses, Argo CD triggers, and the webhook secret cache."""

import json
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from tests.trustbridge.github.conftest import Env, FakeChecker, KeySigner, make_host_config
from trustbridge.adapters.interface import Tenant
from trustbridge.github.availability import HostMonitor, MonitorDeps
from trustbridge.github.checks import (
    CheckRunReporter,
    ScanResult,
    manual_action_report,
    scan_report,
)
from trustbridge.github.deployments import (
    ArgoCdDeploymentHandler,
    ArgoTrigger,
    DeploymentReport,
    DeploymentReporter,
    DeploymentState,
    parse_argocd_trigger,
    verify_static_header,
)
from trustbridge.github.webhook_secrets import (
    HostUnregisteredError,
    WebhookSecretResolver,
    WebhookSecretUnavailableError,
)


def repo(env: Env) -> None:
    """Script GET /repositories/10."""
    env.gh.route("GET", "/repositories/10", body={"full_name": "acme/web"})


async def test_scan_result_creates_a_failing_check_run(env: Env) -> None:
    """A critical finding fails the check; the run is created when none exists for the name."""
    repo(env)
    env.gh.route("GET", "/repos/acme/web/commits/abc/check-runs", body={"check_runs": []})
    env.gh.route("POST", "/repos/acme/web/check-runs", 201, {"id": 9})
    report = scan_report(ScanResult(10, "abc", "ghcr.io/acme/web:1", 1, 0, 2, 3, "https://r"))
    run_id = await CheckRunReporter(env.gateway).report(env.tenant, report)
    assert run_id == 9
    body = env.gh.json_of("POST", "/repos/acme/web/check-runs")
    assert body["conclusion"] == "failure"
    assert body["name"] == "trustbridge / registry scan"
    assert body["details_url"] == "https://r"
    assert body["output"]["summary"].startswith("Image `ghcr.io/acme/web:1`: 1 critical")


async def test_reporting_a_check_twice_updates_it_in_place(env: Env) -> None:
    """An existing run for `(sha, name)` is patched, not duplicated."""
    repo(env)
    env.gh.route("GET", "/repos/acme/web/commits/abc/check-runs", body={"check_runs": [{"id": 5}]})
    env.gh.route("PATCH", "/repos/acme/web/check-runs/5", 200, {})
    report = manual_action_report(10, "abc", "dockerhub", "Create the OIDC connection in the UI")
    assert await CheckRunReporter(env.gateway).report(env.tenant, report) == 5
    assert (
        env.gh.json_of("PATCH", "/repos/acme/web/check-runs/5")["conclusion"] == "action_required"
    )


def test_clean_scan_passes_and_high_findings_fail_by_default() -> None:
    """Conclusion mapping."""
    assert scan_report(ScanResult(1, "s", "i", 0, 0, 5, 5)).conclusion == "success"
    assert scan_report(ScanResult(1, "s", "i", 0, 1, 0, 0)).conclusion == "failure"
    assert (
        scan_report(ScanResult(1, "s", "i", 0, 1, 0, 0), fail_on_high=False).conclusion == "success"
    )


async def test_deployment_is_created_once_and_statuses_are_not_repeated(env: Env) -> None:
    """Reporting the same state again appends nothing; a new state appends a status."""
    repo(env)
    statuses: list[dict[str, Any]] = []
    env.gh.route("GET", "/repos/acme/web/deployments", body=[])
    env.gh.route("POST", "/repos/acme/web/deployments", 201, {"id": 3})
    env.gh.on(
        "GET",
        "/repos/acme/web/deployments/3/statuses",
        lambda _r: httpx2.Response(200, json=statuses[-1:]),
    )

    def add_status(request: httpx2.Request) -> httpx2.Response:
        statuses.append(json.loads(request.content))
        return httpx2.Response(201, json={})

    env.gh.on("POST", "/repos/acme/web/deployments/3/statuses", add_status)
    reporter = DeploymentReporter(env.gateway)
    rep = DeploymentReport(10, "abc", "prod", DeploymentState.IN_PROGRESS, log_url="https://argo/x")
    assert await reporter.report(env.tenant, rep) == 3
    await reporter.report(env.tenant, rep)
    assert [s["state"] for s in statuses] == ["in_progress"]
    await reporter.report(env.tenant, DeploymentReport(10, "abc", "prod", DeploymentState.SUCCESS))
    assert [s["state"] for s in statuses] == ["in_progress", "success"]
    assert env.gh.json_of("POST", "/repos/acme/web/deployments")["required_contexts"] == []


async def test_existing_deployment_is_reused(env: Env) -> None:
    """A deployment for `(sha, environment)` is found, not created again."""
    repo(env)
    env.gh.route("GET", "/repos/acme/web/deployments", body=[{"id": 8}])
    env.gh.route("GET", "/repos/acme/web/deployments/8/statuses", body=[])
    env.gh.route("POST", "/repos/acme/web/deployments/8/statuses", 201, {})
    rep = DeploymentReport(10, "abc", "prod", DeploymentState.FAILURE, description="degraded")
    assert await DeploymentReporter(env.gateway).report(env.tenant, rep) == 8
    assert "POST /repos/acme/web/deployments" not in env.gh.paths()


def test_argocd_payload_is_only_a_pointer_and_the_header_is_compared_in_constant_time() -> None:
    """ADR 0029: a static-header payload never carries state we act on."""
    assert parse_argocd_trigger(
        {"app": "web", "namespace": "argocd", "state": "Synced"}
    ) == ArgoTrigger("web", "argocd")
    assert parse_argocd_trigger({"state": "Synced"}) is None
    assert verify_static_header("Bearer t0k", "Bearer t0k")
    assert not verify_static_header("Bearer t0k", "Bearer other")
    assert not verify_static_header("Bearer t0k", None)


async def test_argocd_handler_reports_the_re_read_state_not_the_payload(env: Env) -> None:
    """The handler asks the source (the Argo CD adapter) for state and reports that."""
    repo(env)
    env.gh.route("GET", "/repos/acme/web/deployments", body=[{"id": 1}])
    env.gh.route("GET", "/repos/acme/web/deployments/1/statuses", body=[])
    env.gh.route("POST", "/repos/acme/web/deployments/1/statuses", 201, {})
    seen: list[ArgoTrigger] = []

    class Source:
        async def observe(self, tenant: Tenant, trigger: ArgoTrigger) -> DeploymentReport | None:
            seen.append(trigger)
            return DeploymentReport(10, "abc", "prod", DeploymentState.SUCCESS)

    handler = ArgoCdDeploymentHandler(Source(), DeploymentReporter(env.gateway))
    assert await handler.handle(env.tenant, {"app": "web", "state": "Failed"}) == 1
    assert env.gh.json_of("POST", "/repos/acme/web/deployments/1/statuses")["state"] == "success"
    assert seen == [ArgoTrigger("web")]
    assert await handler.handle(env.tenant, {}) is None


class Reader:
    """A `SecretReader` that can go down."""

    def __init__(self) -> None:
        """Start up."""
        self.down = False
        self.reads = 0

    async def read(self, ref: str) -> SecretStr:
        """Return a secret, or fail."""
        self.reads += 1
        if self.down:
            raise RuntimeError("vault down")
        return SecretStr(f"secret-of-{ref}")


async def test_webhook_secret_is_cached_after_the_first_read() -> None:
    """Invariant 11: an outage after a successful read does not stop verification."""
    mon = HostMonitor(
        [make_host_config()], {"main": KeySigner()}, MonitorDeps(checker=FakeChecker())
    )
    await mon.check_all()
    reader = Reader()
    resolver = WebhookSecretResolver(mon, reader)
    assert await resolver.secret_for("github.com") == b"secret-of-ref/webhook"
    reader.down = True
    assert await resolver.secret_for("github.com") == b"secret-of-ref/webhook"
    assert reader.reads == 1


async def test_uncached_secret_during_an_outage_and_unregistered_hosts_are_distinct_errors() -> (
    None
):
    """No cache and the store down: 503-class error; unregistered host: its own error."""
    mon = HostMonitor(
        [make_host_config("a"), make_host_config("b", app_id=None)],
        {"main": KeySigner()},
        MonitorDeps(checker=FakeChecker()),
    )
    await mon.check_all()
    reader = Reader()
    reader.down = True
    resolver = WebhookSecretResolver(mon, reader)
    with pytest.raises(WebhookSecretUnavailableError):
        await resolver.secret_for("a")
    with pytest.raises(HostUnregisteredError):
        await resolver.secret_for("b")
