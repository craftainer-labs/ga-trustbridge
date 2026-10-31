"""E2E journey test: what the "security" role can and can't do end-to-end."""

from collections.abc import Callable

from playwright.sync_api import Page


def test_security_has_audit_access_but_no_hero_access(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """Security can read /audit but is denied hero access across every format."""
    headers = {"Authorization": f"Bearer {access_token('security')}"}

    audit_response = page.request.get(f"{base_url}/audit", headers=headers)
    assert audit_response.ok
    assert audit_response.json()["roles"] == ["security"]

    list_response = page.request.get(
        f"{base_url}/crud/v1/heroes/v2/json", headers=headers, fail_on_status_code=False
    )
    assert list_response.status == 403

    list_xml_response = page.request.get(
        f"{base_url}/crud/v1/heroes/v2/xml", headers=headers, fail_on_status_code=False
    )
    assert list_xml_response.status == 403

    form_response = page.request.get(
        f"{base_url}/crud/v1/heroes/v2/web/form", headers=headers, fail_on_status_code=False
    )
    assert form_response.status == 403

    create_response = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/json",
        data={"name": "Nobody", "powers": ["None"]},
        headers=headers,
        fail_on_status_code=False,
    )
    assert create_response.status == 403
