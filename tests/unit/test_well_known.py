"""Unit test: /.well-known/oauth-protected-resource (RFC 9728) and security.txt (RFC 9116)."""

from datetime import UTC, datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.http_headers import add_security_headers
from app.main import app
from app.well_known import build_well_known_router, render_security_txt


def _client(settings: Settings) -> TestClient:
    well_known_app = FastAPI()
    add_security_headers(well_known_app)
    well_known_app.include_router(build_well_known_router(settings))
    return TestClient(well_known_app)


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_name": "demo",
        "oidc_issuer": "http://localhost:8080/realms/demo",
        "oidc_discovery_url": "http://keycloak:8080/realms/demo/.well-known/openid-configuration",
        "public_base_url": "https://api.example.com/",
    }
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


def test_protected_resource_metadata_is_built_from_configuration() -> None:
    """The document names the public issuer (not the internal discovery host) and the resource."""
    response = _client(_settings()).get(
        "/.well-known/oauth-protected-resource", headers={"Host": "evil.example"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "resource": "https://api.example.com",
        "authorization_servers": ["http://localhost:8080/realms/demo"],
        "scopes_supported": ["openid"],
        "bearer_methods_supported": ["header"],
        "resource_name": "demo",
    }


def test_protected_resource_metadata_carries_security_headers() -> None:
    """The security-headers middleware covers the new endpoint."""
    response = _client(_settings()).get("/.well-known/oauth-protected-resource")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" in response.headers


def test_real_app_serves_the_metadata_unauthenticated() -> None:
    """Mounted on the real app, next to the api-catalog, with no token required."""
    response = TestClient(app).get("/.well-known/oauth-protected-resource")
    assert response.status_code == 200
    assert response.json()["bearer_methods_supported"] == ["header"]


def test_security_txt_is_not_served_without_a_contact() -> None:
    """Off unless SECURITY_TXT_CONTACT is configured."""
    assert _client(_settings()).get("/.well-known/security.txt").status_code == 404
    assert render_security_txt(_settings()) is None


def test_security_txt_renders_every_configured_field() -> None:
    """RFC 9116 field lines, Expires in UTC, plain text, with security headers."""
    expires = (datetime.now(UTC) + timedelta(days=90)).replace(microsecond=0)
    settings = _settings(
        security_txt_contact="mailto:security@example.com",
        security_txt_expires=expires.astimezone(timezone(timedelta(hours=2))),
        security_txt_preferred_languages="en, de",
        security_txt_policy="https://example.com/policy",
        security_txt_canonical="https://api.example.com/.well-known/security.txt",
    )
    response = _client(settings).get("/.well-known/security.txt")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.text == (
        "Contact: mailto:security@example.com\n"
        f"Expires: {expires.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        "Preferred-Languages: en, de\n"
        "Policy: https://example.com/policy\n"
        "Canonical: https://api.example.com/.well-known/security.txt\n"
    )


def test_security_txt_minimal_has_only_contact_and_expires() -> None:
    """Optional fields are omitted when unset."""
    settings = _settings(
        security_txt_contact="https://github.com/craftainer/x/security/advisories/new",
        security_txt_expires=datetime.now(UTC) + timedelta(days=30),
    )
    lines = (render_security_txt(settings) or "").splitlines()
    assert [line.split(":")[0] for line in lines] == ["Contact", "Expires"]
