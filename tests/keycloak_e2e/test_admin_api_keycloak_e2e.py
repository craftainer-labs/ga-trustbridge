"""The admin API against tokens minted by a real Keycloak (ADR 0033, NFR-0030).

CI-only (`.github/workflows/keycloak-e2e.yml`): that workflow starts Keycloak with this
directory's realm fixture and runs this file with `OIDC_ISSUER_URL` pointing at it. The app side
is the real admin router and the real `app.oidc.get_current_claims` (discovery, JWKS, signature,
`exp`, `iss`, `aud`) over the in-memory fakes of `tests.trustbridge.admin.helpers`; only the
database read of one tenant is patched. What it adds to the synthetic-token tests is the claim
shape Keycloak really emits for namespaced client roles.
"""

import os
import time
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.oidc import get_current_claims, settings
from tests.trustbridge.admin import helpers
from tests.trustbridge.admin.helpers import ACME, OTHER, build
from trustbridge.admin import routes
from trustbridge.tenancy import TenantInfo

KEYCLOAK = os.environ.get("KEYCLOAK_E2E_URL", "http://localhost:8080")
REALM = "trustbridge-e2e"
TOKEN_URL = f"{KEYCLOAK}/realms/{REALM}/protocol/openid-connect/token"
HOSTS = "/admin/v1/hosts"


def _token(username: str, client_id: str = "e2e-cli") -> str:
    """A real access token by password grant (the fixture users' password is their name)."""
    response = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "password",
            "client_id": client_id,
            "username": username,
            "password": username,
        },
        timeout=15,
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The admin app with the real token validation (the helpers' fake claims are removed)."""
    assert settings.expected_issuer == f"{KEYCLOAK}/realms/{REALM}", (
        "run through .github/workflows/keycloak-e2e.yml: MODE=dev and OIDC_ISSUER_URL must "
        "name the test realm before pytest starts"
    )
    env = build()
    env.app.dependency_overrides.pop(get_current_claims)

    async def get_tenant(_factory: object, tenant_id: Any) -> TenantInfo | None:  # noqa: ANN401
        ref = next((t for t in (ACME, OTHER) if t.id == tenant_id), None)
        if ref is None:
            return None
        return TenantInfo(ref.id, ref.host, 7, ref.org or "", 70, "active", None, False)

    monkeypatch.setattr(routes, "get_tenant", get_tenant)
    with TestClient(env.app, client=("testclient", 50000)) as test_client:
        yield test_client


def test_a_viewer_reads_and_cannot_mutate(client: TestClient) -> None:
    """Instance `viewer`: GET works; a mutation is a 403 problem (inv. 2, 5)."""
    headers = _bearer(_token("kc-viewer"))

    assert client.get(HOSTS, headers=headers).status_code == 200
    denied = client.post(
        f"/admin/v1/tenants/{ACME.id}/disable", json={"reason": "hold"}, headers=headers
    )
    helpers.problem(denied, 403)


def test_the_ladder_holds_for_a_real_operator(client: TestClient) -> None:
    """`operator` reads, but a disable needs `admin`."""
    headers = _bearer(_token("kc-operator"))

    assert client.get(HOSTS, headers=headers).status_code == 200
    denied = client.post(
        f"/admin/v1/tenants/{ACME.id}/disable", json={"reason": "hold"}, headers=headers
    )
    assert denied.status_code == 403


def test_a_tenant_scoped_role_reaches_only_its_tenant(client: TestClient) -> None:
    """`...:acme:viewer` reads acme; another tenant and an unknown id are the same 404."""
    headers = _bearer(_token("kc-acme-viewer"))

    assert client.get(f"/admin/v1/tenants/{ACME.id}", headers=headers).status_code == 200
    other = helpers.problem(client.get(f"/admin/v1/tenants/{OTHER.id}", headers=headers), 404)
    unknown = helpers.problem(client.get(f"/admin/v1/tenants/{uuid4()}", headers=headers), 404)
    assert other["detail"] == unknown["detail"]
    # an instance-scoped route rejects a tenant-scoped grant
    assert client.get(HOSTS, headers=headers).status_code == 403


def test_a_tenant_scoped_admin_cannot_reach_instance_actions(client: TestClient) -> None:
    """A tenant `admin` is refused a disable (instance `admin` only), on its own tenant too."""
    headers = _bearer(_token("kc-acme-admin"))

    denied = client.post(
        f"/admin/v1/tenants/{ACME.id}/disable", json={"reason": "hold"}, headers=headers
    )
    assert denied.status_code == 403
    assert client.get(f"/admin/v1/tenants/{OTHER.id}", headers=headers).status_code == 404


def test_an_instance_admin_reads_every_tenant(client: TestClient) -> None:
    """The instance grant covers both tenants."""
    headers = _bearer(_token("kc-admin"))

    for tenant in (ACME, OTHER):
        assert client.get(f"/admin/v1/tenants/{tenant.id}", headers=headers).status_code == 200


def test_no_token_and_a_garbage_token_are_401(client: TestClient) -> None:
    """The challenge names the RFC 6750 error only when a token was presented."""
    missing = client.get(HOSTS)
    garbage = client.get(HOSTS, headers=_bearer("not.a.jwt"))

    assert missing.status_code == 401
    assert 'error="invalid_token"' not in missing.headers["www-authenticate"]
    assert garbage.status_code == 401
    assert 'error="invalid_token"' in garbage.headers["www-authenticate"]


def test_a_token_for_another_audience_is_401(client: TestClient) -> None:
    """A genuine token whose `aud` is another resource server is refused."""
    assert client.get(HOSTS, headers=_bearer(_token("kc-viewer"))).status_code == 200

    wrong = client.get(HOSTS, headers=_bearer(_token("kc-viewer", "e2e-other-audience")))

    assert wrong.status_code == 401
    assert 'error="invalid_token"' in wrong.headers["www-authenticate"]


def test_an_expired_token_is_401(client: TestClient) -> None:
    """A two-second token works at first and is refused once it has expired."""
    # the first request fetches discovery and the JWKS, which must not eat the token's two seconds
    assert client.get(HOSTS, headers=_bearer(_token("kc-viewer"))).status_code == 200
    token = _token("kc-viewer", "e2e-short")
    assert client.get(HOSTS, headers=_bearer(token)).status_code == 200

    time.sleep(3.5)

    expired = client.get(HOSTS, headers=_bearer(token))
    assert expired.status_code == 401
    assert 'error="invalid_token"' in expired.headers["www-authenticate"]
