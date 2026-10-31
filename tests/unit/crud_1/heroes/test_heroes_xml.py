"""Unit test: /crud/v1/heroes/v2/xml CRUD routes, with the Hero repository faked out."""

from craftainer_crud.repositories.memory import InMemoryRepository
from fastapi.testclient import TestClient

from app.crud_1.heroes.heroes_v2 import get_hero_crud
from app.main import app
from app.models.hero import Hero as HeroModel

from .conftest import override_hero_crud

client = TestClient(app)


def test_hero_xml_crud_lifecycle(authed: None) -> None:
    """Create, list, get, update, and delete a hero through the XML routes."""
    repository = InMemoryRepository(HeroModel)
    app.dependency_overrides[get_hero_crud] = override_hero_crud(repository)
    try:
        create_response = client.post(
            "/crud/v1/heroes/v2/xml",
            content="<hero><name>Spider-Man</name><powers>Wall-crawling</powers></hero>",
            headers={"Content-Type": "application/xml"},
        )
        assert create_response.status_code == 201
        assert create_response.headers["content-type"] == "application/xml"
        assert "<name>Spider-Man</name>" in create_response.text

        list_response = client.get("/crud/v1/heroes/v2/xml")
        assert list_response.status_code == 200
        assert "<heroes>" in list_response.text
        hero_id = list_response.text.split("<id>")[1].split("</id>")[0]

        get_response = client.get("/crud/v1/heroes/v2/xml", params={"id": hero_id})
        assert get_response.status_code == 200
        assert "<name>Spider-Man</name>" in get_response.text

        update_response = client.patch(
            "/crud/v1/heroes/v2/xml",
            params={"id": hero_id},
            content="<hero><powers>Web-slinging</powers><powers>Wall-crawling</powers></hero>",
            headers={"Content-Type": "application/xml"},
        )
        assert update_response.status_code == 200
        assert "<powers>Web-slinging</powers><powers>Wall-crawling</powers>" in update_response.text

        delete_response = client.delete("/crud/v1/heroes/v2/xml", params={"id": hero_id})
        assert delete_response.status_code == 204

        missing_response = client.get("/crud/v1/heroes/v2/xml", params={"id": hero_id})
        assert missing_response.status_code == 404
    finally:
        del app.dependency_overrides[get_hero_crud]


def test_hero_xml_update_missing_returns_404(authed: None) -> None:
    """PATCH /crud/v1/heroes/v2/xml?id= for a nonexistent id returns 404."""
    app.dependency_overrides[get_hero_crud] = override_hero_crud(InMemoryRepository(HeroModel))
    try:
        response = client.patch(
            "/crud/v1/heroes/v2/xml",
            params={"id": 999},
            content="<hero><name>Nobody</name></hero>",
            headers={"Content-Type": "application/xml"},
        )
    finally:
        del app.dependency_overrides[get_hero_crud]
    assert response.status_code == 404


def test_hero_xml_delete_missing_returns_404(authed: None) -> None:
    """DELETE /crud/v1/heroes/v2/xml?id= for a nonexistent id returns 404."""
    app.dependency_overrides[get_hero_crud] = override_hero_crud(InMemoryRepository(HeroModel))
    try:
        response = client.delete("/crud/v1/heroes/v2/xml", params={"id": 999})
    finally:
        del app.dependency_overrides[get_hero_crud]
    assert response.status_code == 404


def test_hero_xml_create_rejects_a_billion_laughs_payload(authed: None) -> None:
    """POST xml rejects a nested-entity-expansion payload with 400, not a hang/OOM."""
    app.dependency_overrides[get_hero_crud] = override_hero_crud(InMemoryRepository(HeroModel))
    billion_laughs = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ELEMENT lolz (#PCDATA)>
 <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
]>
<hero><name>&lol2;</name></hero>"""
    try:
        response = client.post(
            "/crud/v1/heroes/v2/xml",
            content=billion_laughs,
            headers={"Content-Type": "application/xml"},
        )
    finally:
        del app.dependency_overrides[get_hero_crud]
    assert response.status_code == 400
