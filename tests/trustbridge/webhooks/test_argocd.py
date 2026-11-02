"""The Argo CD notification route (ADR 0029 `STATIC_HEADER`: per-tenant bearer, constant time)."""

import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from tests.trustbridge.helpers import make_monitor, make_settings
from trustbridge.adapters.interface import Tenant
from trustbridge.github.availability import HostNotReadyError, HostState, HostStatus
from trustbridge.github.deployments import ArgoCdDeploymentHandler
from trustbridge.github.webhook_secrets import WebhookSecretResolver
from trustbridge.webhooks.argocd import ADAPTER, TOKEN_KEY, router
from trustbridge.webhooks.ingress import ArgocdRuntime, WebhookRuntime
from trustbridge.webhooks.ingress import router as ingress_router
from trustbridge.webhooks.limiter import InMemoryWindowCounter, WebhookLimiter
from trustbridge.worker.deliveries import InMemoryDeliveryStore
from trustbridge.worker.ports import ConnectionData, MemoryTenants
from trustbridge.worker.secrets import MemorySecrets

TOKEN = "argo-token"  # noqa: S105 -- test value


class StubHandler(ArgoCdDeploymentHandler):
    """Records what the route hands over; optionally fails like a host that is not ready."""

    def __init__(self) -> None:
        """Initialise."""
        self.calls: list[tuple[Tenant, Mapping[str, Any]]] = []
        self.not_ready = False

    async def handle(self, tenant: Tenant, payload: Mapping[str, Any]) -> int | None:
        """Handle."""
        if self.not_ready:
            raise HostNotReadyError("github.com", HostStatus(HostState.UNAVAILABLE, "down"))
        self.calls.append((tenant, payload))
        return 41


class Setup:
    """An app with both webhook routers and one tenant that has a notification token."""

    def __init__(self, *, with_handler: bool = True, **settings_kw: Any) -> None:  # noqa: ANN401
        """Initialise."""
        self.settings = make_settings(**settings_kw)
        self.handler = StubHandler()
        monitor = make_monitor(self.settings.github_hosts)
        projection = {h.name: h.to_host() for h in self.settings.github_hosts}
        self.tenants = MemoryTenants(lambda name: projection[name])
        self.secrets = MemorySecrets()
        self.connections = ConnectionData(self.secrets)
        self.tenant_id: UUID | None = None
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.include_router(ingress_router)
        self.runtime = WebhookRuntime(
            settings=self.settings,
            monitor=monitor,
            secrets=WebhookSecretResolver(monitor, self.secrets),
            store=InMemoryDeliveryStore(),
            limiter=WebhookLimiter(
                counter=InMemoryWindowCounter(),
                enabled=False,
                requests=10,
                window_seconds=60,
                timeout_ms=50,
            ),
            argocd=ArgocdRuntime(
                handler=self.handler if with_handler else None,
                tenants=self.tenants,
                configs=self.connections,
            ),
        )
        self.app.state.trustbridge = self.runtime

    async def start(self, *, token: str | None = TOKEN) -> Setup:
        """Start."""
        self.tenant_id = await self.tenants.upsert_installation("github.com", 7, "acme", 1)
        if token is not None:
            await self.connections.put(self.tenant_id, ADAPTER, {TOKEN_KEY: SecretStr(token)})
        return self

    async def post(
        self,
        body: bytes = b'{"app": "web"}',
        *,
        tenant_id: UUID | str | None = None,
        authorization: str | None = f"Bearer {TOKEN}",
    ) -> httpx.Response:
        """Post."""
        headers = {} if authorization is None else {"authorization": authorization}
        path = f"/webhooks/argocd/{tenant_id or self.tenant_id}"
        transport = httpx.ASGITransport(app=self.app, client=("203.0.113.5", 1))
        async with httpx.AsyncClient(transport=transport, base_url="http://tb") as client:
            return await client.post(path, content=body, headers=headers)


async def test_a_notification_with_the_tenants_token_reports_the_re_read_state() -> None:
    """A notification with the tenants token reports the re read state."""
    tb = await Setup().start()
    response = await tb.post()
    assert response.status_code == 202
    assert response.json() == {"deployment_id": 41}
    tenant, payload = tb.handler.calls[0]
    assert (tenant.id, tenant.org, payload) == (tb.tenant_id, "acme", {"app": "web"})


@pytest.mark.parametrize(
    "authorization", [None, "Bearer wrong", "Basic argo-token", "Bearer", f"bearer {TOKEN}"]
)
async def test_a_missing_or_wrong_token_is_401_and_nothing_is_reported(
    authorization: str | None,
) -> None:
    """A missing or wrong token is 401 and nothing is reported."""
    tb = await Setup().start()
    assert (await tb.post(authorization=authorization)).status_code == 401
    assert tb.handler.calls == []


async def test_an_unknown_tenant_and_a_tenant_without_a_token_look_the_same() -> None:
    """An unknown tenant and a tenant without a token look the same."""
    tb = await Setup().start(token=None)
    assert (await tb.post()).status_code == 401  # no token configured for the tenant
    unknown = await tb.post(tenant_id="00000000-0000-4000-8000-000000000001")
    assert unknown.status_code == 401
    assert unknown.json() == (await tb.post()).json()


async def test_without_a_deployment_state_source_the_route_answers_503() -> None:
    """Without a deployment state source the route answers 503."""
    tb = await Setup(with_handler=False).start()
    assert (await tb.post()).status_code == 503
    tb.runtime.argocd = None
    assert (await tb.post()).status_code == 503


@pytest.mark.parametrize("body", [b"\xff not json", b"[1]"])
async def test_a_body_that_is_not_a_json_object_is_400(body: bytes) -> None:
    """A body that is not a json object is 400."""
    tb = await Setup().start()
    assert (await tb.post(body)).status_code == 400
    assert tb.handler.calls == []


async def test_a_body_over_the_cap_is_413() -> None:
    """A body over the cap is 413."""
    tb = await Setup(webhook_max_body_bytes=10).start()
    assert (await tb.post(json.dumps({"app": "x" * 50}).encode())).status_code == 413


async def test_a_host_that_is_not_ready_is_503_so_argocd_retries() -> None:
    """A host that is not ready is 503 so argocd retries."""
    tb = await Setup().start()
    tb.handler.not_ready = True
    assert (await tb.post()).status_code == 503
