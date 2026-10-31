"""E2E smoke test: /crud/v1/heroes/v1/web/form and web/components.js against the live api."""

from collections.abc import Callable

from playwright.sync_api import Page


def test_hero_v1_form_serves_html_and_accepts_a_submission(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """GET web/form serves HTML; POSTing to it creates a hero and redirects back."""
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}

    form_response = page.request.get(f"{base_url}/crud/v1/heroes/v1/web/form", headers=headers)
    assert form_response.ok
    assert "<form" in form_response.text()

    submit_response = page.request.post(
        f"{base_url}/crud/v1/heroes/v1/web/form",
        form={"name": "Storm", "superpower": "Weather control"},
        headers=headers,
    )
    assert submit_response.ok  # Playwright follows the 303 redirect by default
    assert submit_response.url == f"{base_url}/crud/v1/heroes/v1/web/form"


def test_hero_v1_components_js_serves_javascript(page: Page, base_url: str) -> None:
    """GET /crud/v1/heroes/v1/web/components.js serves the web-component JS, unauthenticated."""
    response = page.request.get(f"{base_url}/crud/v1/heroes/v1/web/components.js")
    assert response.ok
    assert "customElements.define" in response.text()
