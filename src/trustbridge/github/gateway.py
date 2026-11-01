"""Shared plumbing for GitHub calls made on behalf of a tenant: token, requester, repo lookup."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from trustbridge.adapters.interface import Tenant
from trustbridge.github.availability import HostMonitor
from trustbridge.github.client import ApiResponse, GitHubApi, GitHubRequester
from trustbridge.github.hosts import GitHubHostConfig
from trustbridge.github.ports import InstallationResolver
from trustbridge.github.tokens import InstallationTokenProvider


def enc(segment: str) -> str:
    """Percent-encode one URL path segment (environment and secret names)."""
    return quote(segment, safe="")


@dataclass(frozen=True, slots=True)
class GatewayDeps:
    """Collaborators of `TenantGateway`."""

    monitor: HostMonitor
    tokens: InstallationTokenProvider
    installations: InstallationResolver
    api_factory: Callable[[GitHubHostConfig], GitHubRequester] | None = None


@dataclass(frozen=True, slots=True)
class Call:
    """One GitHub request and the down-scoping its token needs."""

    method: str
    path: str
    permissions: Mapping[str, str]
    repository_ids: Sequence[int] | None = None
    json: Mapping[str, Any] | None = None
    params: Mapping[str, Any] | None = None


class TenantGateway:
    """Calls GitHub for one tenant with a token down-scoped to repository and permission."""

    def __init__(self, deps: GatewayDeps) -> None:
        """Keep the collaborators and an empty repository-name cache."""
        self._d = deps
        self._api = deps.api_factory or GitHubApi
        self._full_names: dict[tuple[str, int], str] = {}

    async def call(self, tenant: Tenant, request: Call) -> ApiResponse:
        """Send one request with a freshly down-scoped (or cached) installation token."""
        installation = await self._d.installations.installation_id(tenant.id)
        host = tenant.github_host.name
        token = await self._d.tokens.token(
            host,
            installation,
            repository_ids=request.repository_ids,
            permissions=request.permissions,
        )
        cfg = self._d.monitor.config(host)
        return await self._api(cfg).request(
            request.method, request.path, token=token, json=request.json, params=request.params
        )

    async def full_name(self, tenant: Tenant, repository_id: int) -> str:
        """Resolve `owner/name` for a durable repository id (identity is the id, never the name)."""
        key = (tenant.github_host.name, repository_id)
        cached = self._full_names.get(key)
        if cached is not None:
            return cached
        resp = await self.call(
            tenant,
            Call(
                "GET",
                f"/repositories/{repository_id}",
                {"metadata": "read"},
                repository_ids=[repository_id],
            ),
        )
        name = str(resp.data["full_name"])
        self._full_names[key] = name
        return name

    def forget_repository(self, tenant: Tenant, repository_id: int) -> None:
        """Drop a cached name (after a rename or transfer event)."""
        self._full_names.pop((tenant.github_host.name, repository_id), None)
