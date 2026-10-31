"""E2E test: a real bearer token is accepted, a malformed one is rejected with 401.

Replaces the coverage the deleted /protected example route's own e2e test gave
app.oidc.decode_bearer_token/get_current_claims's rejection path -- exercised here
against a real role-gated resource route instead (GET /crud/v1/heroes/v2/json).
"""

from collections.abc import Callable

from playwright.sync_api import Page


def test_role_gated_route_accepts_a_real_token(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """GET a role-gated route with a real bearer token succeeds for a "viewer"-roled user."""
    response = page.request.get(
        f"{base_url}/crud/v1/heroes/v2/json",
        headers={"Authorization": f"Bearer {access_token('viewer')}"},
    )
    assert response.ok


def test_role_gated_route_rejects_a_malformed_token(page: Page, base_url: str) -> None:
    """GET a role-gated route with a present but malformed bearer token is rejected with 401."""
    response = page.request.get(
        f"{base_url}/crud/v1/heroes/v2/json",
        headers={"Authorization": "Bearer not-a-jwt"},
        fail_on_status_code=False,
    )
    assert response.status == 401
