"""Unit test: POST /mock/token issues a Keycloak-shaped mock access token.

Tests the router directly (not via app.main.app) since app.controllers.mock is
only mounted there when MODE=mock -- see app.main's _mount_mode_specific_routers.
"""

import jwt
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.controllers.mock import router

app = FastAPI()
app.include_router(router, prefix="/mock")
client = TestClient(app)


def test_issue_mock_token_carries_the_given_sub_and_roles() -> None:
    """POST /mock/token returns a token whose claims match the request."""
    response = client.post("/mock/token", json={"sub": "detective", "roles": ["detective"]})
    assert response.status_code == 200
    token = response.json()["access_token"]
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["sub"] == "detective"
    settings = get_settings()
    assert claims["resource_access"][settings.oidc_client_id]["roles"] == ["detective"]


def test_issue_mock_token_defaults_to_no_roles() -> None:
    """POST /mock/token without roles issues a token with an empty roles list."""
    response = client.post("/mock/token", json={"sub": "viewer"})
    assert response.status_code == 200
    token = response.json()["access_token"]
    claims = jwt.decode(token, options={"verify_signature": False})
    settings = get_settings()
    assert claims["resource_access"][settings.oidc_client_id]["roles"] == []
