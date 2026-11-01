"""Deployments and deployment statuses from Argo CD notifications.

Argo CD notifications reach the App as an inbound webhook authenticated by a per-tenant static
header (ADR 0029: `STATIC_HEADER`, compared in constant time). Such a payload is never trusted
for state: `parse_argocd_trigger` extracts only which application changed, and the state is
re-read through the platform (the Argo CD adapter, NG5, implements `DeploymentStateSource`) before
`DeploymentReporter` writes a GitHub deployment and status.
"""

import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from trustbridge.adapters.interface import Tenant
from trustbridge.github.gateway import Call, TenantGateway


class DeploymentState(StrEnum):
    """GitHub deployment status states trustbridge reports."""

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILURE = "failure"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class DeploymentReport:
    """The observed state of a rollout of `sha` to `environment`."""

    repository_id: int
    sha: str
    environment: str
    state: DeploymentState
    description: str | None = None
    log_url: str | None = None
    environment_url: str | None = None


@dataclass(frozen=True, slots=True)
class ArgoTrigger:
    """What a notification identifies: only a pointer, never state."""

    app: str
    namespace: str | None = None
    project: str | None = None


class DeploymentStateSource(Protocol):
    """Re-reads an application's rollout state through Argo CD (implemented by NG5's adapter)."""

    async def observe(self, tenant: Tenant, trigger: ArgoTrigger) -> DeploymentReport | None:
        """Return the current rollout state, or None when the app is unknown or has no sha."""
        ...


def verify_static_header(expected: str, received: str | None) -> bool:
    """Constant-time comparison of a static webhook header (per-tenant bearer)."""
    if received is None:
        return False
    return hmac.compare_digest(expected.encode(), received.encode())


def parse_argocd_trigger(payload: Mapping[str, Any]) -> ArgoTrigger | None:
    """Extract the application pointer from a notification body; None if it names no app."""
    app = payload.get("app") or payload.get("application")
    if not isinstance(app, str) or not app:
        return None
    namespace = payload.get("namespace")
    project = payload.get("project")
    return ArgoTrigger(
        app=app,
        namespace=namespace if isinstance(namespace, str) else None,
        project=project if isinstance(project, str) else None,
    )


class DeploymentReporter:
    """Creates the GitHub deployment for `(sha, environment)` once and appends statuses to it."""

    def __init__(self, gateway: TenantGateway) -> None:
        """Use `gateway` for tokens and requests."""
        self._gw = gateway

    async def report(self, tenant: Tenant, report: DeploymentReport) -> int:
        """Record `report`; repeating it appends nothing. Returns the deployment id."""
        full = await self._gw.full_name(tenant, report.repository_id)
        ids = [report.repository_id]
        perms = {"deployments": "write"}
        existing = await self._gw.call(
            tenant,
            Call(
                "GET",
                f"/repos/{full}/deployments",
                perms,
                ids,
                params={"sha": report.sha, "environment": report.environment, "per_page": 1},
            ),
        )
        if existing.data:
            deployment_id = int(existing.data[0]["id"])
        else:
            created = await self._gw.call(
                tenant,
                Call(
                    "POST",
                    f"/repos/{full}/deployments",
                    perms,
                    ids,
                    {
                        "ref": report.sha,
                        "environment": report.environment,
                        "auto_merge": False,
                        "required_contexts": [],
                        "description": "Argo CD rollout",
                    },
                ),
            )
            deployment_id = int(created.data["id"])
        latest = await self._gw.call(
            tenant,
            Call(
                "GET",
                f"/repos/{full}/deployments/{deployment_id}/statuses",
                perms,
                ids,
                params={"per_page": 1},
            ),
        )
        if latest.data and latest.data[0].get("state") == report.state.value:
            return deployment_id
        body: dict[str, object] = {"state": report.state.value}
        if report.description:
            body["description"] = report.description[:140]
        if report.log_url:
            body["log_url"] = report.log_url
        if report.environment_url:
            body["environment_url"] = report.environment_url
        await self._gw.call(
            tenant,
            Call("POST", f"/repos/{full}/deployments/{deployment_id}/statuses", perms, ids, body),
        )
        return deployment_id


class ArgoCdDeploymentHandler:
    """Turns a verified notification trigger into a deployment status via a re-read."""

    def __init__(self, source: DeploymentStateSource, reporter: DeploymentReporter) -> None:
        """Combine the platform re-read and the GitHub reporter."""
        self._source = source
        self._reporter = reporter

    async def handle(self, tenant: Tenant, payload: Mapping[str, Any]) -> int | None:
        """Re-read state for the notified app and report it; None when nothing to report."""
        trigger = parse_argocd_trigger(payload)
        if trigger is None:
            return None
        observed = await self._source.observe(tenant, trigger)
        if observed is None:
            return None
        return await self._reporter.report(tenant, observed)
