"""E2E journey test: what the "maintainer" role can and can't do end-to-end."""

from collections.abc import Callable

from playwright.sync_api import Page


def test_maintainer_has_full_hero_lifecycle_but_not_audit(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """A maintainer has full CRUD across every hero format but is denied /audit."""
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}

    create_response = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/json",
        data={"name": "Doctor Strange", "powers": ["Mystic arts"]},
        headers=headers,
    )
    assert create_response.status == 201
    hero_id = create_response.json()["id"]

    form_response = page.request.get(f"{base_url}/crud/v1/heroes/v2/web/form", headers=headers)
    assert form_response.ok
    assert "<form" in form_response.text()

    list_response = page.request.get(f"{base_url}/crud/v1/heroes/v2/json", headers=headers)
    assert list_response.ok
    assert any(hero["id"] == hero_id for hero in list_response.json())

    xml_update_response = page.request.patch(
        f"{base_url}/crud/v1/heroes/v2/xml",
        params={"id": hero_id},
        data="<hero><powers>Time manipulation</powers></hero>",
        headers={**headers, "Content-Type": "application/xml"},
    )
    assert xml_update_response.ok

    get_response = page.request.get(
        f"{base_url}/crud/v1/heroes/v2/json", params={"id": hero_id}, headers=headers
    )
    assert get_response.ok
    assert get_response.json()["powers"] == ["Time manipulation"]

    delete_response = page.request.delete(
        f"{base_url}/crud/v1/heroes/v2/json", params={"id": hero_id}, headers=headers
    )
    assert delete_response.status == 204

    audit_response = page.request.get(
        f"{base_url}/audit", headers=headers, fail_on_status_code=False
    )
    assert audit_response.status == 403
