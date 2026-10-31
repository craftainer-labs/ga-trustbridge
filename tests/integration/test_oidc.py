"""Integration test: validate a real access token against the live Keycloak.

Unlike tests/unit/, this reaches the actual `keycloak` stack
container (already running under the devcontainer, and under CI's
devcontainers/ci) instead of mocking it -- exercising the same discovery
+ JWKS code path app.oidc.decode_bearer_token uses for a real request.
"""

import httpx
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.oidc import decode_bearer_token

client = TestClient(app)


def _fetch_access_token() -> str:
    """Obtain a real access token for the dev realm's "viewer" user."""
    settings = get_settings()
    # The in-network address (the discovery URL's realm), not settings.oidc_token_url:
    # that one is the browser-facing localhost URL Swagger UI uses, unreachable from here.
    token_url = (
        settings.discovery_url.removesuffix("/.well-known/openid-configuration")
        + "/protocol/openid-connect/token"
    )
    response = httpx.post(
        token_url,
        data={
            "grant_type": "password",
            "client_id": "test-cli",
            "username": "viewer",
            "password": "viewer",
        },
        timeout=10,
    )
    response.raise_for_status()
    return response.json()["access_token"]  # type: ignore[no-any-return]


def test_decode_bearer_token_accepts_a_real_keycloak_token() -> None:
    """A token obtained from the real Keycloak dev realm decodes successfully."""
    claims = decode_bearer_token(_fetch_access_token())

    assert claims["preferred_username"] == "viewer"


def test_role_gated_route_accepts_a_real_keycloak_token() -> None:
    """GET a role-gated route with a real bearer token succeeds for a "viewer"-roled user."""
    access_token = _fetch_access_token()

    response = client.get(
        "/crud/v1/heroes/v2/json", headers={"Authorization": f"Bearer {access_token}"}
    )

    assert response.status_code == 200
