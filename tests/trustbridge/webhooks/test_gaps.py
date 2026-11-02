"""Webhook edges: an app without the runtime installed, and exempt-address handling."""

import ipaddress

import httpx
from fastapi import FastAPI

from trustbridge.webhooks.ingress import router
from trustbridge.webhooks.limiter import InMemoryWindowCounter, WebhookLimiter


async def test_the_route_answers_503_until_the_runtime_is_installed() -> None:
    """An app whose lifespan has not set up the runtime reports itself not ready."""
    app = FastAPI()
    app.include_router(router)
    transport = httpx.ASGITransport(app=app, client=("203.0.113.5", 1))
    async with httpx.AsyncClient(transport=transport, base_url="http://tb") as client:
        response = await client.post("/webhooks/github/ok", content=b"{}")
    assert response.status_code == 503


async def test_exempt_networks_match_mapped_ipv4_and_a_missing_address_is_counted() -> None:
    """`::ffff:a.b.c.d` is checked against IPv4 networks; no address is limited like any other."""
    limiter = WebhookLimiter(
        counter=InMemoryWindowCounter(),
        enabled=True,
        requests=1,
        window_seconds=60,
        timeout_ms=50,
        exempt=[ipaddress.ip_network("10.0.0.0/8")],
    )
    for _ in range(3):
        assert (await limiter.check("::ffff:10.1.2.3")).allowed
    assert (await limiter.check(None)).allowed
    assert not (await limiter.check(None)).allowed
