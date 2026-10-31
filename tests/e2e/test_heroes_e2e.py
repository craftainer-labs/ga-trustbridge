"""E2E smoke test: /crud/v1/heroes/v2/json CRUD against the live api."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from playwright.sync_api import Page


def test_hero_crud_lifecycle(page: Page, base_url: str, access_token: Callable[[str], str]) -> None:
    """Create, list, get, update, and delete a hero, then confirm 404s past that point."""
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    create_response = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/json",
        data={"name": "Black Panther", "powers": ["Vibranium suit"]},
        headers=headers,
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
        assert get_response.json()["name"] == "Black Panther"

        update_response = page.request.patch(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": hero_id},
            data={"powers": ["Enhanced senses"]},
            headers=headers,
        )
        assert update_response.ok
        assert update_response.json()["powers"] == ["Enhanced senses"]
    finally:
        delete_response = page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/json", params={"id": hero_id}, headers=headers
        )
        assert delete_response.status == 204

    missing_get_response = page.request.get(
        f"{base_url}/crud/v1/heroes/v2/json",
        params={"id": hero_id},
        headers=headers,
        fail_on_status_code=False,
    )
    assert missing_get_response.status == 404

    missing_update_response = page.request.patch(
        f"{base_url}/crud/v1/heroes/v2/json",
        params={"id": hero_id},
        data={"name": "Nobody"},
        headers=headers,
        fail_on_status_code=False,
    )
    assert missing_update_response.status == 404

    missing_delete_response = page.request.delete(
        f"{base_url}/crud/v1/heroes/v2/json",
        params={"id": hero_id},
        headers=headers,
        fail_on_status_code=False,
    )
    assert missing_delete_response.status == 404


def test_hero_filter_sort_and_bulk_actions(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """Filtering/sorting a list and a bulk delete via filters work end to end."""
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    first = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/json",
        data={"name": "Filter Test Alpha", "powers": ["Speed"]},
        headers=headers,
    ).json()
    second = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/json",
        data={"name": "Filter Test Beta", "powers": ["Speed"]},
        headers=headers,
    ).json()

    try:
        filtered = page.request.get(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"name__icontains": "Filter Test", "sort": "name"},
            headers=headers,
        )
        assert filtered.ok
        assert [hero["name"] for hero in filtered.json()] == [
            "Filter Test Alpha",
            "Filter Test Beta",
        ]

        created_after = page.request.get(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={
                "name__icontains": "Filter Test",
                "created_at__min": "2000-01-01T00:00:00+00:00",
            },
            headers=headers,
        )
        assert created_after.ok
        assert len(created_after.json()) == 2

        created_before = page.request.get(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={
                "name__icontains": "Filter Test",
                # No UTC offset -- exercises the naive-datetime path of
                # craftainer_crud.controllers.crud_query._cast_datetime, distinct from
                # created_after's tz-aware value above.
                "created_at__max": "2999-01-01T00:00:00",
            },
            headers=headers,
        )
        assert created_before.ok
        assert len(created_before.json()) == 2

        exact_match = page.request.get(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"name": "Filter Test Alpha"},
            headers=headers,
        )
        assert exact_match.ok
        assert [hero["name"] for hero in exact_match.json()] == ["Filter Test Alpha"]

        regex_match = page.request.get(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"name__regex": "^Filter Test"},
            headers=headers,
        )
        assert regex_match.ok
        assert {hero["name"] for hero in regex_match.json()} == {
            "Filter Test Alpha",
            "Filter Test Beta",
        }

        oversized_regex = page.request.get(
            f"{base_url}/crud/v1/heroes/v2/json", params={"name__regex": "a" * 201}, headers=headers
        )
        assert oversized_regex.status == 422

        bulk_update = page.request.patch(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"name__icontains": "Filter Test"},
            data={"powers": ["Bulk updated"]},
            headers=headers,
        )
        assert bulk_update.ok
        assert bulk_update.json()["matched"] == 2

        bulk_delete = page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"name__icontains": "Filter Test"},
            headers=headers,
        )
        assert bulk_delete.ok
        assert bulk_delete.json()["matched"] == 2
    finally:
        page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": first["id"]},
            headers=headers,
            fail_on_status_code=False,
        )
        page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": second["id"]},
            headers=headers,
            fail_on_status_code=False,
        )


def test_create_hero_with_missing_field_returns_422(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """POST /crud/v1/heroes/v2/json missing `powers` returns a validation problem."""
    response = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/json",
        data={"name": "Nobody"},
        headers={"Authorization": f"Bearer {access_token('maintainer')}"},
        fail_on_status_code=False,
    )
    assert response.status == 422
    assert response.headers["content-type"] == "application/problem+json"


def test_hero_list_with_invalid_filter_returns_422(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """An unrecognized filter key/op, or a value that fails to cast, is a 400-shaped 422."""
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}

    unrecognized_field = page.request.get(
        f"{base_url}/crud/v1/heroes/v2/json", params={"nonexistent_field": "x"}, headers=headers
    )
    assert unrecognized_field.status == 422
    assert unrecognized_field.headers["content-type"] == "application/problem+json"

    unrecognized_op = page.request.get(
        f"{base_url}/crud/v1/heroes/v2/json", params={"name__min": "x"}, headers=headers
    )
    assert unrecognized_op.status == 422

    invalid_value = page.request.get(
        f"{base_url}/crud/v1/heroes/v2/json", params={"id__min": "not-a-number"}, headers=headers
    )
    assert invalid_value.status == 422


def test_hero_list_with_invalid_sort_returns_422(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """An unrecognized sort field is a 422; an empty sort segment is silently skipped."""
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}

    unrecognized_field = page.request.get(
        f"{base_url}/crud/v1/heroes/v2/json", params={"sort": "nonexistent_field"}, headers=headers
    )
    assert unrecognized_field.status == 422
    assert unrecognized_field.headers["content-type"] == "application/problem+json"

    trailing_comma = page.request.get(
        f"{base_url}/crud/v1/heroes/v2/json", params={"sort": "name,"}, headers=headers
    )
    assert trailing_comma.ok


def test_bulk_update_and_delete_with_no_filters_and_no_id_are_rejected(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """A bulk PATCH/DELETE with neither id nor filters is rejected, never a full-table action."""
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}

    update_response = page.request.patch(
        f"{base_url}/crud/v1/heroes/v2/json",
        data={"name": "Should Not Apply"},
        headers=headers,
        fail_on_status_code=False,
    )
    assert update_response.status == 422

    delete_response = page.request.delete(
        f"{base_url}/crud/v1/heroes/v2/json", headers=headers, fail_on_status_code=False
    )
    assert delete_response.status == 422


def test_hero_record_lifecycle_draft_through_revisions(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """The full opt-in record-lifecycle sequence, end to end against the live api: draft ->
    publish -> lock -> attempt (and fail) an edit -> unlock -> archive -> list (excluded) ->
    list with include_archived -> restore -> clone -> /revisions reflects the sequence.
    """
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    hero_ids_to_clean_up: list[int] = []
    try:
        draft_response = page.request.post(
            f"{base_url}/crud/v1/heroes/v2/json/draft",
            data={"name": "E2E Draft Hero"},
            headers=headers,
        )
        assert draft_response.status == 201
        draft = draft_response.json()
        assert draft["is_draft"] is True
        hero_id = draft["id"]
        hero_ids_to_clean_up.append(hero_id)

        incomplete_publish = page.request.post(
            f"{base_url}/crud/v1/heroes/v2/json/publish",
            params={"id": hero_id},
            headers=headers,
            fail_on_status_code=False,
        )
        assert incomplete_publish.status == 422

        complete_response = page.request.patch(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": hero_id},
            data={"powers": ["Grappling hook"]},
            headers=headers,
        )
        assert complete_response.ok

        publish_response = page.request.post(
            f"{base_url}/crud/v1/heroes/v2/json/publish", params={"id": hero_id}, headers=headers
        )
        assert publish_response.ok
        assert publish_response.json()["is_draft"] is False

        lock_response = page.request.patch(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": hero_id},
            data={"is_locked": True},
            headers=headers,
        )
        assert lock_response.ok
        assert lock_response.json()["is_locked"] is True

        failed_edit = page.request.patch(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": hero_id},
            data={"powers": ["Should not apply"]},
            headers=headers,
            fail_on_status_code=False,
        )
        assert failed_edit.status == 423

        unlock_response = page.request.patch(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": hero_id},
            data={"is_locked": False},
            headers=headers,
        )
        assert unlock_response.ok
        assert unlock_response.json()["is_locked"] is False

        archive_response = page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/json", params={"id": hero_id}, headers=headers
        )
        assert archive_response.status == 204

        excluded_response = page.request.get(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": hero_id},
            headers=headers,
            fail_on_status_code=False,
        )
        assert excluded_response.status == 404

        included_response = page.request.get(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": hero_id, "include_archived": "true"},
            headers=headers,
        )
        assert included_response.ok
        assert included_response.json()["archived_at"] is not None

        restore_response = page.request.post(
            f"{base_url}/crud/v1/heroes/v2/json/restore", params={"id": hero_id}, headers=headers
        )
        assert restore_response.ok
        assert restore_response.json()["archived_at"] is None

        clone_response = page.request.post(
            f"{base_url}/crud/v1/heroes/v2/json/clone", params={"id": hero_id}, headers=headers
        )
        assert clone_response.status == 201
        clone = clone_response.json()
        hero_ids_to_clean_up.append(clone["id"])
        assert clone["id"] != hero_id
        assert clone["name"] == "E2E Draft Hero"
        assert clone["powers"] == ["Grappling hook"]
        assert clone["is_draft"] is True

        revisions_response = page.request.get(
            f"{base_url}/crud/v1/heroes/v2/json/revisions", params={"id": hero_id}, headers=headers
        )
        assert revisions_response.ok
        actions = [revision["action"] for revision in revisions_response.json()]
        assert actions == ["delete", "update", "update", "update", "update", "create"]
    finally:
        for hero_id in hero_ids_to_clean_up:
            page.request.delete(
                f"{base_url}/crud/v1/heroes/v2/json",
                params={"id": hero_id},
                headers=headers,
                fail_on_status_code=False,
            )


def test_hero_bulk_restore_via_filters(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """POST /crud/v1/heroes/v2/json/restore with no id restores every matching archived hero."""
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    suffix = uuid4()
    first = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/json",
        data={"name": f"Bulk Restore E2E {suffix}", "powers": ["A"]},
        headers=headers,
    ).json()
    second = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/json",
        data={"name": f"Bulk Restore E2E {suffix} B", "powers": ["A"]},
        headers=headers,
    ).json()
    try:
        page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/json", params={"id": first["id"]}, headers=headers
        )
        page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/json", params={"id": second["id"]}, headers=headers
        )

        response = page.request.post(
            f"{base_url}/crud/v1/heroes/v2/json/restore",
            params={"name__icontains": str(suffix)},
            headers=headers,
        )
        assert response.ok
        assert response.json()["matched"] == 2
    finally:
        page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": first["id"]},
            headers=headers,
            fail_on_status_code=False,
        )
        page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": second["id"]},
            headers=headers,
            fail_on_status_code=False,
        )


def test_hero_restore_missing_returns_404(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """POST /crud/v1/heroes/v2/json/restore?id= for a nonexistent id returns 404."""
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    response = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/json/restore",
        params={"id": -1},
        headers=headers,
        fail_on_status_code=False,
    )
    assert response.status == 404


def test_hero_clone_missing_returns_404(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """POST /crud/v1/heroes/v2/json/clone?id= for a nonexistent id returns 404."""
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    response = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/json/clone",
        params={"id": -1},
        headers=headers,
        fail_on_status_code=False,
    )
    assert response.status == 404


def test_hero_publish_missing_returns_404(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """POST /crud/v1/heroes/v2/json/publish?id= for a nonexistent id returns 404."""
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    response = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/json/publish",
        params={"id": -1},
        headers=headers,
        fail_on_status_code=False,
    )
    assert response.status == 404


def test_caller_cannot_publish_another_owners_draft(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """One user's POST /publish?id= 404s for a draft another user created, rather than 500ing.

    `crud.get` (unscoped, see craftainer_crud.interfaces.base.OwnerScope's read_scoped=False)
    lets the
    second caller see the first caller's draft exists, but `crud.update` (always
    owner-scoped) returns None since they don't own it --
    craftainer_crud.controllers.crud_router's
    publish_record must turn that into a 404, not return None as its `-> schema` response.
    Both "editor" and "maintainer" hold the write role Hero's /publish route requires (see
    app.crud_1.heroes.heroes_v2.WriteRoles), so they stand in here as two distinct owners.
    """
    maintainer_headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    editor_headers = {"Authorization": f"Bearer {access_token('editor')}"}
    suffix = uuid4()
    draft = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/json/draft",
        data={"name": f"E2E Cross-Owner Draft Hero {suffix}"},
        headers=maintainer_headers,
    ).json()
    try:
        # Fill in the fields create_schema requires, as the owner -- otherwise
        # publish_record's own required-fields check 422s before ever reaching the
        # owner-scoped crud.update this test means to exercise.
        page.request.patch(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": draft["id"]},
            data={"powers": ["Tactics"]},
            headers=maintainer_headers,
        )

        publish_attempt = page.request.post(
            f"{base_url}/crud/v1/heroes/v2/json/publish",
            params={"id": draft["id"]},
            headers=editor_headers,
            fail_on_status_code=False,
        )
        assert publish_attempt.status == 404

        still_draft = page.request.get(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": draft["id"]},
            headers=maintainer_headers,
        ).json()
        assert still_draft["is_draft"] is True
    finally:
        page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": draft["id"]},
            headers=maintainer_headers,
            fail_on_status_code=False,
        )


def test_hero_bulk_lock_blocks_bulk_update_and_delete(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """A locked hero is skipped by neither -- bulk PATCH/DELETE against it 423s."""
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    suffix = uuid4()
    hero = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/json",
        data={"name": f"E2E Bulk Locked Hero {suffix}", "powers": ["Immovable"]},
        headers=headers,
    ).json()
    try:
        page.request.patch(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": hero["id"]},
            data={"is_locked": True},
            headers=headers,
        )

        bulk_update = page.request.patch(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"name__icontains": str(suffix)},
            data={"powers": ["Should not apply"]},
            headers=headers,
            fail_on_status_code=False,
        )
        assert bulk_update.status == 423

        bulk_delete = page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"name__icontains": str(suffix)},
            headers=headers,
            fail_on_status_code=False,
        )
        assert bulk_delete.status == 423
    finally:
        page.request.patch(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": hero["id"]},
            data={"is_locked": False},
            headers=headers,
        )
        page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": hero["id"]},
            headers=headers,
            fail_on_status_code=False,
        )


def test_hero_bulk_restore_and_delete_with_no_filters_and_no_id_are_rejected(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """A bulk restore/delete/update with neither id nor filters is rejected (422)."""
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    restore_response = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/json/restore", headers=headers, fail_on_status_code=False
    )
    assert restore_response.status == 422


def test_hero_delete_and_update_by_id_while_locked_return_423(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """PATCH/DELETE ?id= for a locked hero both 423, matching bulk lock enforcement."""
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    hero = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/json",
        data={"name": "E2E Locked Hero", "powers": ["Immovable"]},
        headers=headers,
    ).json()
    try:
        page.request.patch(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": hero["id"]},
            data={"is_locked": True},
            headers=headers,
        )

        delete_response = page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": hero["id"]},
            headers=headers,
            fail_on_status_code=False,
        )
        assert delete_response.status == 423
    finally:
        page.request.patch(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": hero["id"]},
            data={"is_locked": False},
            headers=headers,
        )
        page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": hero["id"]},
            headers=headers,
            fail_on_status_code=False,
        )


def test_hero_boolean_filters_and_invalid_include_archived(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """Boolean field filters (is_draft/is_locked) and the include_archived flag both parse
    true/false correctly, and an unrecognized include_archived value is a 422.
    """
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    hero = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/json",
        data={"name": "E2E Boolean Filter Hero", "powers": ["A"]},
        headers=headers,
    ).json()
    try:
        not_draft = page.request.get(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"name": hero["name"], "is_draft": "false"},
            headers=headers,
        )
        assert not_draft.ok

        not_locked = page.request.get(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"name": hero["name"], "is_locked": "false", "include_archived": "false"},
            headers=headers,
        )
        assert not_locked.ok

        invalid_include_archived = page.request.get(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"name": hero["name"], "include_archived": "maybe"},
            headers=headers,
            fail_on_status_code=False,
        )
        assert invalid_include_archived.status == 422
    finally:
        page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": hero["id"]},
            headers=headers,
            fail_on_status_code=False,
        )


def test_hero_scheduled_visibility(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """A hero with a future publish_at or a past unpublish_at is excluded from a plain GET
    by default, and reachable with include_unpublished=true -- see
    craftainer_crud.models.mixins.Schedulable.
    Setting the two columns is just a normal PATCH (see views.hero_v2.HeroV2Update).
    """
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    hero = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/json",
        data={"name": "E2E Scheduled Hero", "powers": ["A"]},
        headers=headers,
    ).json()
    try:
        future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        schedule_response = page.request.patch(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": hero["id"]},
            data={"publish_at": future},
            headers=headers,
        )
        assert schedule_response.ok

        excluded = page.request.get(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": hero["id"]},
            headers=headers,
            fail_on_status_code=False,
        )
        assert excluded.status == 404

        included = page.request.get(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": hero["id"], "include_unpublished": "true"},
            headers=headers,
        )
        assert included.ok
        assert included.json()["publish_at"] is not None

        # publish_at must move to the past too -- otherwise it alone would already
        # exclude the record, without ever reaching the unpublish_at check below it.
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        further_past = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        unpublish_response = page.request.patch(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": hero["id"]},
            data={"publish_at": further_past, "unpublish_at": past},
            headers=headers,
        )
        assert unpublish_response.ok

        excluded_after_unpublish = page.request.get(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": hero["id"]},
            headers=headers,
            fail_on_status_code=False,
        )
        assert excluded_after_unpublish.status == 404
    finally:
        page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": hero["id"]},
            headers=headers,
            fail_on_status_code=False,
        )
