"""E2E smoke test: /crud/v1/heroes/v2/xml CRUD against the live api."""

from collections.abc import Callable
from uuid import uuid4

from playwright.sync_api import Page


def test_hero_xml_crud_lifecycle(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """Create, list, get, update, and delete a hero through the XML routes."""
    headers = {
        "Authorization": f"Bearer {access_token('maintainer')}",
        "Content-Type": "application/xml",
    }
    create_response = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/xml",
        data="<hero><name>Storm</name><powers>Weather control</powers></hero>",
        headers=headers,
    )
    assert create_response.status == 201
    body = create_response.text()
    hero_id = body.split("<id>")[1].split("</id>")[0]

    try:
        list_response = page.request.get(f"{base_url}/crud/v1/heroes/v2/xml", headers=headers)
        assert list_response.ok
        assert f"<id>{hero_id}</id>" in list_response.text()

        get_response = page.request.get(
            f"{base_url}/crud/v1/heroes/v2/xml", params={"id": hero_id}, headers=headers
        )
        assert get_response.ok
        assert "<name>Storm</name>" in get_response.text()

        update_response = page.request.patch(
            f"{base_url}/crud/v1/heroes/v2/xml",
            params={"id": hero_id},
            data="<hero><powers>Lightning storms</powers></hero>",
            headers=headers,
        )
        assert update_response.ok
        assert "<powers>Lightning storms</powers>" in update_response.text()
    finally:
        delete_response = page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/xml", params={"id": hero_id}, headers=headers
        )
        assert delete_response.status == 204

    missing_response = page.request.get(
        f"{base_url}/crud/v1/heroes/v2/xml",
        params={"id": hero_id},
        headers=headers,
        fail_on_status_code=False,
    )
    assert missing_response.status == 404


def test_hero_xml_update_missing_returns_404(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """PATCH /crud/v1/heroes/v2/xml?id= for a nonexistent id returns 404."""
    headers = {
        "Authorization": f"Bearer {access_token('maintainer')}",
        "Content-Type": "application/xml",
    }
    response = page.request.patch(
        f"{base_url}/crud/v1/heroes/v2/xml",
        params={"id": 999999},
        data="<hero><name>Nobody</name></hero>",
        headers=headers,
        fail_on_status_code=False,
    )
    assert response.status == 404


def test_hero_xml_delete_missing_returns_404(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """DELETE /crud/v1/heroes/v2/xml?id= for a nonexistent id returns 404."""
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    response = page.request.delete(
        f"{base_url}/crud/v1/heroes/v2/xml",
        params={"id": 999999},
        headers=headers,
        fail_on_status_code=False,
    )
    assert response.status == 404


def test_hero_xml_create_rejects_a_billion_laughs_payload(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """POST xml rejects a nested-entity-expansion payload with 400, not a hang/OOM."""
    billion_laughs = """<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ELEMENT lolz (#PCDATA)>
 <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
]>
<hero><name>&lol2;</name></hero>"""
    response = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/xml",
        data=billion_laughs,
        headers={
            "Authorization": f"Bearer {access_token('maintainer')}",
            "Content-Type": "application/xml",
        },
        fail_on_status_code=False,
    )
    assert response.status == 400


def test_hero_xml_record_lifecycle_draft_through_revisions(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """XML counterpart of test_heroes_e2e.py's own
    test_hero_record_lifecycle_draft_through_revisions: draft -> publish -> lock ->
    attempt (and fail) an edit -> unlock -> archive -> list (excluded) -> list with
    include_archived -> restore -> clone -> /revisions reflects the sequence.

    Closes the coverage build_xml_router's own draft/publish/restore/clone/revisions
    routes would otherwise leave on crud_router.py -- only their JSON siblings were
    exercised end to end before this.
    """
    headers = {
        "Authorization": f"Bearer {access_token('maintainer')}",
        "Content-Type": "application/xml",
    }
    hero_ids_to_clean_up: list[int] = []
    try:
        draft_response = page.request.post(
            f"{base_url}/crud/v1/heroes/v2/xml/draft",
            data="<hero><name>E2E XML Draft Hero</name></hero>",
            headers=headers,
        )
        assert draft_response.status == 201
        draft_body = draft_response.text()
        assert "<is_draft>True</is_draft>" in draft_body
        hero_id = int(draft_body.split("<id>")[1].split("</id>")[0])
        hero_ids_to_clean_up.append(hero_id)

        incomplete_publish = page.request.post(
            f"{base_url}/crud/v1/heroes/v2/xml/publish",
            params={"id": hero_id},
            headers=headers,
            fail_on_status_code=False,
        )
        assert incomplete_publish.status == 422

        complete_response = page.request.patch(
            f"{base_url}/crud/v1/heroes/v2/xml",
            params={"id": hero_id},
            data="<hero><powers>Grappling hook</powers></hero>",
            headers=headers,
        )
        assert complete_response.ok

        publish_response = page.request.post(
            f"{base_url}/crud/v1/heroes/v2/xml/publish", params={"id": hero_id}, headers=headers
        )
        assert publish_response.ok
        assert "<is_draft>False</is_draft>" in publish_response.text()

        lock_response = page.request.patch(
            f"{base_url}/crud/v1/heroes/v2/xml",
            params={"id": hero_id},
            data="<hero><is_locked>true</is_locked></hero>",
            headers=headers,
        )
        assert lock_response.ok
        assert "<is_locked>True</is_locked>" in lock_response.text()

        failed_edit = page.request.patch(
            f"{base_url}/crud/v1/heroes/v2/xml",
            params={"id": hero_id},
            data="<hero><powers>Should not apply</powers></hero>",
            headers=headers,
            fail_on_status_code=False,
        )
        assert failed_edit.status == 423

        unlock_response = page.request.patch(
            f"{base_url}/crud/v1/heroes/v2/xml",
            params={"id": hero_id},
            data="<hero><is_locked>false</is_locked></hero>",
            headers=headers,
        )
        assert unlock_response.ok
        assert "<is_locked>False</is_locked>" in unlock_response.text()

        archive_response = page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/xml", params={"id": hero_id}, headers=headers
        )
        assert archive_response.status == 204

        excluded_response = page.request.get(
            f"{base_url}/crud/v1/heroes/v2/xml",
            params={"id": hero_id},
            headers=headers,
            fail_on_status_code=False,
        )
        assert excluded_response.status == 404

        included_response = page.request.get(
            f"{base_url}/crud/v1/heroes/v2/xml",
            params={"id": hero_id, "include_archived": "true"},
            headers=headers,
        )
        assert included_response.ok
        assert "<archived_at>" in included_response.text()

        restore_response = page.request.post(
            f"{base_url}/crud/v1/heroes/v2/xml/restore", params={"id": hero_id}, headers=headers
        )
        assert restore_response.ok
        assert "<archived_at>" not in restore_response.text()

        clone_response = page.request.post(
            f"{base_url}/crud/v1/heroes/v2/xml/clone", params={"id": hero_id}, headers=headers
        )
        assert clone_response.status == 201
        clone_body = clone_response.text()
        clone_id = int(clone_body.split("<id>")[1].split("</id>")[0])
        hero_ids_to_clean_up.append(clone_id)
        assert clone_id != hero_id
        assert "<name>E2E XML Draft Hero</name>" in clone_body
        assert "<powers>Grappling hook</powers>" in clone_body
        assert "<is_draft>True</is_draft>" in clone_body

        revisions_response = page.request.get(
            f"{base_url}/crud/v1/heroes/v2/xml/revisions", params={"id": hero_id}, headers=headers
        )
        assert revisions_response.ok
        actions_xml = revisions_response.text()
        assert actions_xml.count("<action>") == 6
        assert actions_xml.index("<action>delete</action>") < actions_xml.index(
            "<action>create</action>"
        )
    finally:
        for hero_id in hero_ids_to_clean_up:
            page.request.delete(
                f"{base_url}/crud/v1/heroes/v2/xml",
                params={"id": hero_id},
                headers=headers,
                fail_on_status_code=False,
            )


def test_hero_xml_bulk_update_and_delete_via_filters(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """PATCH/DELETE xml?<filters> act in bulk and render an XML bulk-result body."""
    headers = {
        "Authorization": f"Bearer {access_token('maintainer')}",
        "Content-Type": "application/xml",
    }
    page.request.post(
        f"{base_url}/crud/v1/heroes/v2/xml",
        data="<hero><name>XML Bulk Test Alpha</name><powers>Speed</powers></hero>",
        headers=headers,
    )
    page.request.post(
        f"{base_url}/crud/v1/heroes/v2/xml",
        data="<hero><name>XML Bulk Test Beta</name><powers>Speed</powers></hero>",
        headers=headers,
    )

    update_response = page.request.patch(
        f"{base_url}/crud/v1/heroes/v2/xml",
        params={"name__icontains": "XML Bulk Test"},
        data="<hero><powers>Updated</powers></hero>",
        headers=headers,
    )
    assert update_response.ok
    assert "<bulk-update-result>" in update_response.text()
    assert "<matched>2</matched>" in update_response.text()

    delete_response = page.request.delete(
        f"{base_url}/crud/v1/heroes/v2/xml",
        params={"name__icontains": "XML Bulk Test"},
        headers=headers,
    )
    assert delete_response.ok
    assert "<bulk-delete-result>" in delete_response.text()
    assert "<matched>2</matched>" in delete_response.text()


def test_hero_xml_clone_missing_returns_404(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """POST /crud/v1/heroes/v2/xml/clone?id= for a nonexistent id returns 404."""
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    response = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/xml/clone",
        params={"id": -1},
        headers=headers,
        fail_on_status_code=False,
    )
    assert response.status == 404


def test_hero_xml_publish_missing_returns_404(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """POST /crud/v1/heroes/v2/xml/publish?id= for a nonexistent id returns 404."""
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    response = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/xml/publish",
        params={"id": -1},
        headers=headers,
        fail_on_status_code=False,
    )
    assert response.status == 404


def test_caller_cannot_publish_another_owners_draft_xml(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """XML counterpart of test_heroes_e2e.py's own test_caller_cannot_publish_another_owners_draft:
    one user's POST /publish?id= 404s for a draft another user created, rather than 500ing --
    closes the coverage that JSON-only version leaves on build_xml_router's own publish_record_xml
    (crud.get's unscoped read finding the record, then crud.update's owner-scoped write
    returning None for it, must still 404 rather than being treated as a successful response).
    """
    maintainer_headers = {
        "Authorization": f"Bearer {access_token('maintainer')}",
        "Content-Type": "application/xml",
    }
    editor_headers = {"Authorization": f"Bearer {access_token('editor')}"}
    suffix = uuid4()
    draft_response = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/xml/draft",
        data=f"<hero><name>E2E XML Cross-Owner Draft Hero {suffix}</name></hero>",
        headers=maintainer_headers,
    )
    draft_id = int(draft_response.text().split("<id>")[1].split("</id>")[0])
    try:
        page.request.patch(
            f"{base_url}/crud/v1/heroes/v2/xml",
            params={"id": draft_id},
            data="<hero><powers>Tactics</powers></hero>",
            headers=maintainer_headers,
        )

        publish_attempt = page.request.post(
            f"{base_url}/crud/v1/heroes/v2/xml/publish",
            params={"id": draft_id},
            headers=editor_headers,
            fail_on_status_code=False,
        )
        assert publish_attempt.status == 404
    finally:
        page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/xml",
            params={"id": draft_id},
            headers=maintainer_headers,
            fail_on_status_code=False,
        )


def test_hero_xml_bulk_restore_via_filters(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """XML counterpart of test_heroes_e2e.py's own test_hero_bulk_restore_via_filters:
    POST /restore with no id restores every matching archived hero, rendering the same
    bulk-update-result XML shape PATCH/DELETE's own bulk routes already use -- closes the
    coverage the JSON-only version leaves on build_xml_router's own restore_records_xml.
    """
    headers = {
        "Authorization": f"Bearer {access_token('maintainer')}",
        "Content-Type": "application/xml",
    }
    suffix = uuid4()
    first = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/xml",
        data=f"<hero><name>XML Bulk Restore {suffix}</name><powers>A</powers></hero>",
        headers=headers,
    )
    second = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/xml",
        data=f"<hero><name>XML Bulk Restore {suffix} B</name><powers>A</powers></hero>",
        headers=headers,
    )
    first_id = first.text().split("<id>")[1].split("</id>")[0]
    second_id = second.text().split("<id>")[1].split("</id>")[0]
    try:
        page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/xml", params={"id": first_id}, headers=headers
        )
        page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/xml", params={"id": second_id}, headers=headers
        )

        response = page.request.post(
            f"{base_url}/crud/v1/heroes/v2/xml/restore",
            params={"name__icontains": str(suffix)},
            headers=headers,
        )
        assert response.ok
        assert "<bulk-update-result>" in response.text()
        assert "<matched>2</matched>" in response.text()
    finally:
        page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/xml",
            params={"id": first_id},
            headers=headers,
            fail_on_status_code=False,
        )
        page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/xml",
            params={"id": second_id},
            headers=headers,
            fail_on_status_code=False,
        )
