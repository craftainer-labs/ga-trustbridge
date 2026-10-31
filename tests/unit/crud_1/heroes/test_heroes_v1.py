"""Unit test: /crud/v1/heroes/v1/json CRUD routes -- the deprecated single-power Hero shape."""

from craftainer_crud.repositories.memory import InMemoryRepository
from fastapi.testclient import TestClient

from app.crud_1.heroes.heroes_v2 import get_hero_crud
from app.main import app
from app.models.hero import Hero as HeroModel

from .conftest import override_hero_crud as _override_crud

client = TestClient(app)


def test_get_hero_v1_with_multiple_powers_returns_first_as_superpower(authed: None) -> None:
    """GET v1?id= on a hero with multiple v2 powers returns `superpower == powers[0]`."""
    repository = InMemoryRepository(HeroModel)
    app.dependency_overrides[get_hero_crud] = _override_crud(repository)
    try:
        create_response = client.post(
            "/crud/v1/heroes/v2/json",
            json={"name": "Storm", "powers": ["Weather control", "Flight"]},
        )
        hero_id = create_response.json()["id"]

        response = client.get("/crud/v1/heroes/v1/json", params={"id": hero_id})
        assert response.status_code == 200
        assert response.json()["superpower"] == "Weather control"
        assert "powers" not in response.json()
    finally:
        del app.dependency_overrides[get_hero_crud]


def test_create_hero_v1_persists_superpower_as_single_element_powers_list(authed: None) -> None:
    """POST v1 with `superpower` reads back via v2 GET as `powers == [superpower]`."""
    repository = InMemoryRepository(HeroModel)
    app.dependency_overrides[get_hero_crud] = _override_crud(repository)
    try:
        create_response = client.post(
            "/crud/v1/heroes/v1/json", json={"name": "Batman", "superpower": "Detective skills"}
        )
        assert create_response.status_code == 201
        hero_id = create_response.json()["id"]

        v2_response = client.get("/crud/v1/heroes/v2/json", params={"id": hero_id})
        assert v2_response.status_code == 200
        assert v2_response.json()["powers"] == ["Detective skills"]
    finally:
        del app.dependency_overrides[get_hero_crud]


def test_update_hero_v1_without_superpower_leaves_v2_powers_untouched(authed: None) -> None:
    """PATCH v1?id= with only `name` leaves a multi-power hero's `powers` untouched."""
    repository = InMemoryRepository(HeroModel)
    app.dependency_overrides[get_hero_crud] = _override_crud(repository)
    try:
        create_response = client.post(
            "/crud/v1/heroes/v2/json",
            json={"name": "Storm", "powers": ["Weather control", "Flight"]},
        )
        hero_id = create_response.json()["id"]

        update_response = client.patch(
            "/crud/v1/heroes/v1/json", params={"id": hero_id}, json={"name": "Ororo Munroe"}
        )
        assert update_response.status_code == 200
        assert update_response.json()["name"] == "Ororo Munroe"

        v2_response = client.get("/crud/v1/heroes/v2/json", params={"id": hero_id})
        assert v2_response.json()["powers"] == ["Weather control", "Flight"]
    finally:
        del app.dependency_overrides[get_hero_crud]


def test_v1_responses_carry_deprecation_headers(authed: None) -> None:
    """Every /crud/v1/heroes/v1/json response carries Sunset/Deprecation/Link headers."""
    repository = InMemoryRepository(HeroModel)
    app.dependency_overrides[get_hero_crud] = _override_crud(repository)
    try:
        response = client.get("/crud/v1/heroes/v1/json")
        assert response.status_code == 200
        assert response.headers["Deprecation"] == "true"
        assert "Sunset" in response.headers
        assert response.headers["Link"] == '</crud/v1/heroes/v2>; rel="sunset"'
    finally:
        del app.dependency_overrides[get_hero_crud]


def test_v2_responses_carry_no_deprecation_headers(authed: None) -> None:
    """/crud/v1/heroes/v2/json responses carry none of the /v1 deprecation headers."""
    repository = InMemoryRepository(HeroModel)
    app.dependency_overrides[get_hero_crud] = _override_crud(repository)
    try:
        response = client.get("/crud/v1/heroes/v2/json")
        assert response.status_code == 200
        assert "Deprecation" not in response.headers
        assert "Sunset" not in response.headers
        assert "Link" not in response.headers
    finally:
        del app.dependency_overrides[get_hero_crud]


def test_get_missing_hero_v1_returns_404(authed: None) -> None:
    """GET /crud/v1/heroes/v1/json?id= for a nonexistent id returns 404."""
    app.dependency_overrides[get_hero_crud] = _override_crud(InMemoryRepository(HeroModel))
    try:
        response = client.get("/crud/v1/heroes/v1/json", params={"id": 999})
    finally:
        del app.dependency_overrides[get_hero_crud]
    assert response.status_code == 404


def test_update_missing_hero_v1_returns_404(authed: None) -> None:
    """PATCH /crud/v1/heroes/v1/json?id= for a nonexistent id returns 404."""
    app.dependency_overrides[get_hero_crud] = _override_crud(InMemoryRepository(HeroModel))
    try:
        response = client.patch(
            "/crud/v1/heroes/v1/json", params={"id": 999}, json={"name": "Nobody"}
        )
    finally:
        del app.dependency_overrides[get_hero_crud]
    assert response.status_code == 404


def test_delete_hero_v1(authed: None) -> None:
    """DELETE /crud/v1/heroes/v1/json?id= deletes the underlying v2 record."""
    repository = InMemoryRepository(HeroModel)
    app.dependency_overrides[get_hero_crud] = _override_crud(repository)
    try:
        create_response = client.post(
            "/crud/v1/heroes/v1/json", json={"name": "Nobody", "superpower": "None"}
        )
        hero_id = create_response.json()["id"]

        delete_response = client.delete("/crud/v1/heroes/v1/json", params={"id": hero_id})
        assert delete_response.status_code == 204

        missing_response = client.get("/crud/v1/heroes/v1/json", params={"id": hero_id})
        assert missing_response.status_code == 404
    finally:
        del app.dependency_overrides[get_hero_crud]


def test_delete_missing_hero_v1_returns_404(authed: None) -> None:
    """DELETE /crud/v1/heroes/v1/json?id= for a nonexistent id returns 404."""
    app.dependency_overrides[get_hero_crud] = _override_crud(InMemoryRepository(HeroModel))
    try:
        response = client.delete("/crud/v1/heroes/v1/json", params={"id": 999})
    finally:
        del app.dependency_overrides[get_hero_crud]
    assert response.status_code == 404
