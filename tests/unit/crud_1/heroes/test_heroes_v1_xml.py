"""Unit test: /crud/v1/heroes/v1/xml CRUD -- the deprecated single-power Hero shape, in XML."""

from craftainer_crud.repositories.memory import InMemoryRepository
from fastapi.testclient import TestClient

from app.crud_1.heroes.heroes_v2 import get_hero_crud
from app.main import app
from app.models.hero import Hero as HeroModel

from .conftest import override_hero_crud

client = TestClient(app)


def test_hero_v1_xml_crud_lifecycle(authed: None) -> None:
    """Create, list, get, update, and delete a hero through the v1 XML routes."""
    repository = InMemoryRepository(HeroModel)
    app.dependency_overrides[get_hero_crud] = override_hero_crud(repository)
    try:
        create_response = client.post(
            "/crud/v1/heroes/v1/xml",
            content="<hero><name>Spider-Man</name><superpower>Wall-crawling</superpower></hero>",
            headers={"Content-Type": "application/xml"},
        )
        assert create_response.status_code == 201
        assert create_response.headers["content-type"] == "application/xml"
        assert "<name>Spider-Man</name>" in create_response.text
        assert "<superpower>Wall-crawling</superpower>" in create_response.text

        list_response = client.get("/crud/v1/heroes/v1/xml")
        assert list_response.status_code == 200
        assert "<heroes>" in list_response.text
        hero_id = list_response.text.split("<id>")[1].split("</id>")[0]

        get_response = client.get("/crud/v1/heroes/v1/xml", params={"id": hero_id})
        assert get_response.status_code == 200
        assert "<name>Spider-Man</name>" in get_response.text

        update_response = client.patch(
            "/crud/v1/heroes/v1/xml",
            params={"id": hero_id},
            content="<hero><superpower>Web-slinging</superpower></hero>",
            headers={"Content-Type": "application/xml"},
        )
        assert update_response.status_code == 200
        assert "<superpower>Web-slinging</superpower>" in update_response.text

        delete_response = client.delete("/crud/v1/heroes/v1/xml", params={"id": hero_id})
        assert delete_response.status_code == 204

        missing_response = client.get("/crud/v1/heroes/v1/xml", params={"id": hero_id})
        assert missing_response.status_code == 404
    finally:
        del app.dependency_overrides[get_hero_crud]


def test_hero_v1_xml_update_missing_returns_404(authed: None) -> None:
    """PATCH /crud/v1/heroes/v1/xml?id= for a nonexistent id returns 404."""
    app.dependency_overrides[get_hero_crud] = override_hero_crud(InMemoryRepository(HeroModel))
    try:
        response = client.patch(
            "/crud/v1/heroes/v1/xml",
            params={"id": 999},
            content="<hero><name>Nobody</name></hero>",
            headers={"Content-Type": "application/xml"},
        )
    finally:
        del app.dependency_overrides[get_hero_crud]
    assert response.status_code == 404


def test_hero_v1_xml_delete_missing_returns_404(authed: None) -> None:
    """DELETE /crud/v1/heroes/v1/xml?id= for a nonexistent id returns 404."""
    app.dependency_overrides[get_hero_crud] = override_hero_crud(InMemoryRepository(HeroModel))
    try:
        response = client.delete("/crud/v1/heroes/v1/xml", params={"id": 999})
    finally:
        del app.dependency_overrides[get_hero_crud]
    assert response.status_code == 404


def test_v1_xml_responses_carry_deprecation_headers(authed: None) -> None:
    """Every /crud/v1/heroes/v1/xml response carries Sunset/Deprecation/Link headers."""
    app.dependency_overrides[get_hero_crud] = override_hero_crud(InMemoryRepository(HeroModel))
    try:
        response = client.get("/crud/v1/heroes/v1/xml")
    finally:
        del app.dependency_overrides[get_hero_crud]
    assert response.status_code == 200
    assert response.headers["Deprecation"] == "true"
    assert "Sunset" in response.headers
    assert response.headers["Link"] == '</crud/v1/heroes/v2>; rel="sunset"'
