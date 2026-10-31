"""Unit test: /crud/v1/heroes/v1/web/form and web/components.js -- the deprecated v1 shape."""

from craftainer_crud.repositories.memory import InMemoryRepository
from fastapi.testclient import TestClient

from app.crud_1.heroes.heroes_v2 import get_hero_crud
from app.main import app
from app.models.hero import Hero as HeroModel

from .conftest import override_hero_crud

client = TestClient(app)


def test_hero_v1_form_serves_html(authed: None) -> None:
    """GET /crud/v1/heroes/v1/web/form serves an HTML page with the form and web-component tags."""
    response = client.get("/crud/v1/heroes/v1/web/form")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<form" in response.text
    assert "<hero-list" in response.text


def test_submit_hero_v1_form_creates_a_hero_and_redirects(authed: None) -> None:
    """POST /crud/v1/heroes/v1/web/form creates a hero and redirects back to the form."""
    repository = InMemoryRepository(HeroModel)
    app.dependency_overrides[get_hero_crud] = override_hero_crud(repository)
    try:
        response = client.post(
            "/crud/v1/heroes/v1/web/form",
            data={"name": "Batman", "superpower": "Detective skills"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/crud/v1/heroes/v1/web/form"
    finally:
        del app.dependency_overrides[get_hero_crud]


def test_hero_v1_components_js_defines_custom_elements() -> None:
    """GET /crud/v1/heroes/v1/web/components.js serves the web-component JS, unauthenticated."""
    response = client.get("/crud/v1/heroes/v1/web/components.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert "customElements.define" in response.text


def test_v1_form_page_carries_deprecation_headers(authed: None) -> None:
    """GET /crud/v1/heroes/v1/web/form carries Sunset/Deprecation/Link headers."""
    response = client.get("/crud/v1/heroes/v1/web/form")
    assert response.status_code == 200
    assert response.headers["Deprecation"] == "true"
    assert "Sunset" in response.headers
    assert response.headers["Link"] == '</crud/v1/heroes/v2>; rel="sunset"'
