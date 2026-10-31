"""Unit test: GET /audit -- require_roles gating and its response shape."""

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.oidc import get_current_claims

client = TestClient(app)


def test_audit_requires_security_or_detective_role() -> None:
    """GET /audit with a role outside security/detective is rejected with 403."""
    settings = get_settings()
    app.dependency_overrides[get_current_claims] = lambda: {
        "sub": "u",
        "resource_access": {settings.oidc_client_id: {"roles": ["viewer"]}},
    }
    try:
        response = client.get("/audit")
    finally:
        del app.dependency_overrides[get_current_claims]
    assert response.status_code == 403


def test_audit_returns_subject_and_roles_for_security() -> None:
    """GET /audit returns the caller's subject and granted roles for the security role."""
    settings = get_settings()
    app.dependency_overrides[get_current_claims] = lambda: {
        "sub": "sam",
        "resource_access": {settings.oidc_client_id: {"roles": ["security"]}},
    }
    try:
        response = client.get("/audit")
    finally:
        del app.dependency_overrides[get_current_claims]
    assert response.status_code == 200
    assert response.json() == {"sub": "sam", "roles": ["security"]}
