"""E2E journey test: what the "viewer" role can and can't do end-to-end."""

from collections.abc import Callable, Generator

import pytest
from playwright.sync_api import Page


@pytest.fixture
def seeded_hero_id(page: Page, base_url: str, access_token: Callable[[str], str]) -> Generator[int]:
    """Create a hero as maintainer (viewer can't) and delete it again once the test is done."""
    maintainer_headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    create_response = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/json",
        data={"name": "Iron Fist", "powers": ["Chi mastery"]},
        headers=maintainer_headers,
    )
    assert create_response.status == 201
    hero_id = create_response.json()["id"]
    try:
        yield hero_id
    finally:
        page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/json", params={"id": hero_id}, headers=maintainer_headers
        )


def test_viewer_can_read_but_not_write(
    page: Page, base_url: str, access_token: Callable[[str], str], seeded_hero_id: int
) -> None:
    """A viewer can read heroes across every format but is denied every write and /audit."""
    headers = {"Authorization": f"Bearer {access_token('viewer')}"}

    list_response = page.request.get(f"{base_url}/crud/v1/heroes/v2/json", headers=headers)
    assert list_response.ok
    assert any(hero["id"] == seeded_hero_id for hero in list_response.json())

    get_response = page.request.get(
        f"{base_url}/crud/v1/heroes/v2/json", params={"id": seeded_hero_id}, headers=headers
    )
    assert get_response.ok

    list_xml_response = page.request.get(f"{base_url}/crud/v1/heroes/v2/xml", headers=headers)
    assert list_xml_response.ok
    assert f"<id>{seeded_hero_id}</id>" in list_xml_response.text()

    get_xml_response = page.request.get(
        f"{base_url}/crud/v1/heroes/v2/xml", params={"id": seeded_hero_id}, headers=headers
    )
    assert get_xml_response.ok

    form_response = page.request.get(f"{base_url}/crud/v1/heroes/v2/web/form", headers=headers)
    assert form_response.ok
    assert "<form" in form_response.text()

    create_response = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/json",
        data={"name": "Nobody", "powers": ["None"]},
        headers=headers,
        fail_on_status_code=False,
    )
    assert create_response.status == 403

    update_response = page.request.patch(
        f"{base_url}/crud/v1/heroes/v2/json",
        params={"id": seeded_hero_id},
        data={"powers": ["None"]},
        headers=headers,
        fail_on_status_code=False,
    )
    assert update_response.status == 403

    delete_response = page.request.delete(
        f"{base_url}/crud/v1/heroes/v2/json",
        params={"id": seeded_hero_id},
        headers=headers,
        fail_on_status_code=False,
    )
    assert delete_response.status == 403

    audit_response = page.request.get(
        f"{base_url}/audit", headers=headers, fail_on_status_code=False
    )
    assert audit_response.status == 403
