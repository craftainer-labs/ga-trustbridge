"""E2E smoke test: /crud/v1/heroes/v1/json CRUD against the live api."""

from collections.abc import Callable

from playwright.sync_api import Page


def test_hero_v1_crud_lifecycle(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """Create, list, get, update, and delete a hero through the deprecated v1 routes."""
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    create_response = page.request.post(
        f"{base_url}/crud/v1/heroes/v1/json",
        data={"name": "Black Panther", "superpower": "Vibranium suit"},
        headers=headers,
    )
    assert create_response.status == 201
    hero_id = create_response.json()["id"]

    try:
        list_response = page.request.get(f"{base_url}/crud/v1/heroes/v1/json", headers=headers)
        assert list_response.ok
        assert any(hero["id"] == hero_id for hero in list_response.json())

        get_response = page.request.get(
            f"{base_url}/crud/v1/heroes/v1/json", params={"id": hero_id}, headers=headers
        )
        assert get_response.ok
        assert get_response.json()["name"] == "Black Panther"

        update_response = page.request.patch(
            f"{base_url}/crud/v1/heroes/v1/json",
            params={"id": hero_id},
            data={"superpower": "Enhanced senses"},
            headers=headers,
        )
        assert update_response.ok
        assert update_response.json()["superpower"] == "Enhanced senses"

        v2_response = page.request.get(
            f"{base_url}/crud/v1/heroes/v2/json", params={"id": hero_id}, headers=headers
        )
        assert v2_response.json()["powers"] == ["Enhanced senses"]
    finally:
        delete_response = page.request.delete(
            f"{base_url}/crud/v1/heroes/v1/json", params={"id": hero_id}, headers=headers
        )
        assert delete_response.status == 204

    missing_get_response = page.request.get(
        f"{base_url}/crud/v1/heroes/v1/json",
        params={"id": hero_id},
        headers=headers,
        fail_on_status_code=False,
    )
    assert missing_get_response.status == 404

    missing_update_response = page.request.patch(
        f"{base_url}/crud/v1/heroes/v1/json",
        params={"id": hero_id},
        data={"name": "Nobody"},
        headers=headers,
        fail_on_status_code=False,
    )
    assert missing_update_response.status == 404

    missing_delete_response = page.request.delete(
        f"{base_url}/crud/v1/heroes/v1/json",
        params={"id": hero_id},
        headers=headers,
        fail_on_status_code=False,
    )
    assert missing_delete_response.status == 404


def test_hero_v1_bulk_update_and_delete_via_filters(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """PATCH/DELETE /crud/v1/heroes/v1/json?<filters> act in bulk via CompatCRUD."""
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    page.request.post(
        f"{base_url}/crud/v1/heroes/v1/json",
        data={"name": "V1 Bulk Test Alpha", "superpower": "Speed"},
        headers=headers,
    )
    page.request.post(
        f"{base_url}/crud/v1/heroes/v1/json",
        data={"name": "V1 Bulk Test Beta", "superpower": "Speed"},
        headers=headers,
    )

    update_response = page.request.patch(
        f"{base_url}/crud/v1/heroes/v1/json",
        params={"name__icontains": "V1 Bulk Test"},
        data={"superpower": "Updated"},
        headers=headers,
    )
    assert update_response.ok
    assert update_response.json()["matched"] == 2

    delete_response = page.request.delete(
        f"{base_url}/crud/v1/heroes/v1/json",
        params={"name__icontains": "V1 Bulk Test"},
        headers=headers,
    )
    assert delete_response.ok
    assert delete_response.json()["matched"] == 2


def test_v1_heroes_responses_carry_deprecation_headers(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """v1 responses carry Sunset/Deprecation/Link headers; v2 responses don't."""
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}

    v1_response = page.request.get(f"{base_url}/crud/v1/heroes/v1/json", headers=headers)
    assert v1_response.ok
    assert v1_response.headers["deprecation"] == "true"
    assert "sunset" in v1_response.headers
    assert v1_response.headers["link"] == '</crud/v1/heroes/v2>; rel="sunset"'

    v2_response = page.request.get(f"{base_url}/crud/v1/heroes/v2/json", headers=headers)
    assert v2_response.ok
    assert "deprecation" not in v2_response.headers
    assert "sunset" not in v2_response.headers
