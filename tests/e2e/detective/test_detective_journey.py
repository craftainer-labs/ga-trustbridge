"""E2E journey test: what the "detective" role can and can't do end-to-end."""

from collections.abc import Callable

from playwright.sync_api import Page


def test_detective_can_cross_reference_audit_and_heroes(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """A detective reads both /audit and heroes but can't write to either."""
    headers = {"Authorization": f"Bearer {access_token('detective')}"}
    maintainer_headers = {"Authorization": f"Bearer {access_token('maintainer')}"}

    audit_response = page.request.get(f"{base_url}/audit", headers=headers)
    assert audit_response.ok
    assert audit_response.json()["roles"] == ["detective"]

    create_response = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/json",
        data={"name": "Jessica Jones", "powers": ["Super strength"]},
        headers=maintainer_headers,
    )
    assert create_response.status == 201
    hero_id = create_response.json()["id"]

    try:
        list_response = page.request.get(f"{base_url}/crud/v1/heroes/v2/json", headers=headers)
        assert list_response.ok
        assert any(hero["id"] == hero_id for hero in list_response.json())

        get_response = page.request.get(
            f"{base_url}/crud/v1/heroes/v2/json", params={"id": hero_id}, headers=headers
        )
        assert get_response.ok

        forbidden_create_response = page.request.post(
            f"{base_url}/crud/v1/heroes/v2/json",
            data={"name": "Nobody", "powers": ["None"]},
            headers=headers,
            fail_on_status_code=False,
        )
        assert forbidden_create_response.status == 403

        update_response = page.request.patch(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": hero_id},
            data={"powers": ["None"]},
            headers=headers,
            fail_on_status_code=False,
        )
        assert update_response.status == 403

        delete_response = page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": hero_id},
            headers=headers,
            fail_on_status_code=False,
        )
        assert delete_response.status == 403
    finally:
        page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/json", params={"id": hero_id}, headers=maintainer_headers
        )
