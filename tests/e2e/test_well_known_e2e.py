"""E2E test: RFC 9728 protected-resource metadata, the 401 challenge, and security.txt.

The `dev` leg runs with PUBLIC_BASE_URL and a full SECURITY_TXT_* set; the other legs leave
them unset (see tests/e2e/conftest.py's `_LEG_ENV`), so both the configured and the
default branches run against live processes.
"""

from playwright.sync_api import Page

from tests.e2e.conftest import (
    E2E_PUBLIC_BASE_URL,
    E2E_SECURITY_CONTACT,
    E2E_SECURITY_EXPIRES,
)

_DEFAULT_RESOURCE = "http://localhost:8000"


def _resource(app_leg: str) -> str:
    return E2E_PUBLIC_BASE_URL if app_leg == "dev" else _DEFAULT_RESOURCE


def test_protected_resource_metadata_follows_rfc_9728(
    page: Page, base_url: str, app_leg: str
) -> None:
    """The document needs no token and names the configured resource and issuer."""
    response = page.request.get(
        f"{base_url}/.well-known/oauth-protected-resource", headers={"Host": "evil.example"}
    )
    assert response.ok
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["x-content-type-options"] == "nosniff"
    body = response.json()
    assert body["resource"] == _resource(app_leg)
    assert body["bearer_methods_supported"] == ["header"]
    assert body["scopes_supported"] == ["openid"]
    assert body["resource_name"] == "template-fastapi"
    assert len(body["authorization_servers"]) == 1
    assert body["authorization_servers"][0].endswith("/realms/template-fastapi")


def test_401_points_at_the_metadata_document(page: Page, base_url: str, app_leg: str) -> None:
    """A missing token and a rejected token both carry resource_metadata."""
    expected = (
        f'Bearer resource_metadata="{_resource(app_leg)}/.well-known/oauth-protected-resource"'
    )
    missing = page.request.get(f"{base_url}/crud/v1/heroes/v2/json", fail_on_status_code=False)
    assert missing.status == 401
    assert missing.headers["www-authenticate"] == expected
    bad = page.request.get(
        f"{base_url}/crud/v1/heroes/v2/json",
        headers={"Authorization": "Bearer not-a-jwt"},
        fail_on_status_code=False,
    )
    assert bad.status == 401
    assert bad.headers["www-authenticate"] == f'{expected}, error="invalid_token"'


def test_security_txt_only_when_configured(page: Page, base_url: str, app_leg: str) -> None:
    """404 unless SECURITY_TXT_CONTACT is set (dev leg); then RFC 9116 text/plain."""
    response = page.request.get(f"{base_url}/.well-known/security.txt", fail_on_status_code=False)
    if app_leg != "dev":
        assert response.status == 404
        return
    assert response.ok
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text() == (
        f"Contact: {E2E_SECURITY_CONTACT}\n"
        f"Expires: {E2E_SECURITY_EXPIRES}\n"
        "Preferred-Languages: en, de\n"
        "Policy: https://example.com/security-policy\n"
        "Canonical: https://example.com/.well-known/security.txt\n"
    )
