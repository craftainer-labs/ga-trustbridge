"""Integration test: /crud/v1/heroes/v2/json CRUD routes against the real Postgres stack service."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from starlette.types import Message

from app.config import get_settings
from app.db import session_factory as async_session_factory
from app.main import app
from app.models.hero import Hero
from app.oidc import get_current_claims

client = TestClient(app)


@contextmanager
def _authed_as(sub: str) -> Iterator[None]:
    """Override get_current_claims to authenticate as `sub`, with every RBAC role.

    Restores whatever override was in place on exit, so this can nest inside the
    module's own `_authed` autouse fixture (see that fixture's docstring).
    """
    settings = get_settings()
    previous = app.dependency_overrides.get(get_current_claims)
    app.dependency_overrides[get_current_claims] = lambda: {
        "sub": sub,
        "resource_access": {
            settings.oidc_client_id: {
                "roles": ["viewer", "editor", "maintainer", "security", "detective"]
            }
        },
    }
    try:
        yield
    finally:
        if previous is None:
            del app.dependency_overrides[get_current_claims]
        else:
            app.dependency_overrides[get_current_claims] = previous


@pytest.fixture(autouse=True)
def _authed() -> Iterator[None]:
    """Authenticate every request in this module as "test-user" by default."""
    with _authed_as("test-user"):
        yield


def test_hero_crud_lifecycle_against_real_postgres() -> None:
    """Create, list, get, update, and delete a hero through the live app and real DB."""
    create_response = client.post(
        "/crud/v1/heroes/v2/json", json={"name": "Wonder Woman", "powers": ["Super strength"]}
    )
    assert create_response.status_code == 201
    hero_id = create_response.json()["id"]

    try:
        list_response = client.get("/crud/v1/heroes/v2/json")
        assert list_response.status_code == 200
        assert any(hero["id"] == hero_id for hero in list_response.json())

        get_response = client.get("/crud/v1/heroes/v2/json", params={"id": hero_id})
        assert get_response.status_code == 200
        assert get_response.json()["name"] == "Wonder Woman"

        update_response = client.patch(
            "/crud/v1/heroes/v2/json", params={"id": hero_id}, json={"powers": ["Lasso of truth"]}
        )
        assert update_response.status_code == 200
        assert update_response.json()["powers"] == ["Lasso of truth"]
    finally:
        delete_response = client.delete("/crud/v1/heroes/v2/json", params={"id": hero_id})
        assert delete_response.status_code == 204

    missing_response = client.get("/crud/v1/heroes/v2/json", params={"id": hero_id})
    assert missing_response.status_code == 404


def test_hero_filter_sort_and_bulk_actions_against_real_postgres() -> None:
    """Filtering/sorting a list and bulk update/delete via filters work over the live app."""
    batman = client.post(
        "/crud/v1/heroes/v2/json",
        json={"name": "Batman Filter Test", "powers": ["Detective skills"]},
    ).json()
    batgirl = client.post(
        "/crud/v1/heroes/v2/json",
        json={"name": "Batgirl Filter Test", "powers": ["Detective skills"]},
    ).json()
    superman = client.post(
        "/crud/v1/heroes/v2/json", json={"name": "Superman Filter Test", "powers": ["Flight"]}
    ).json()

    try:
        filtered = client.get("/crud/v1/heroes/v2/json", params={"name__icontains": "Filter Test"})
        assert filtered.status_code == 200
        assert {hero["id"] for hero in filtered.json()} == {
            batman["id"],
            batgirl["id"],
            superman["id"],
        }

        sorted_response = client.get(
            "/crud/v1/heroes/v2/json", params={"name__icontains": "Filter Test", "sort": "name"}
        )
        assert [hero["name"] for hero in sorted_response.json()] == [
            "Batgirl Filter Test",
            "Batman Filter Test",
            "Superman Filter Test",
        ]

        bulk_update = client.patch(
            "/crud/v1/heroes/v2/json",
            params={"name__icontains": "Bat"},
            json={"powers": ["Martial arts"]},
        )
        assert bulk_update.status_code == 200
        body = bulk_update.json()
        assert body["matched"] == 2
        assert set(body["ids"]) == {batman["id"], batgirl["id"]}

        assert client.get("/crud/v1/heroes/v2/json", params={"id": batman["id"]}).json()[
            "powers"
        ] == ["Martial arts"]

        bulk_delete = client.delete("/crud/v1/heroes/v2/json", params={"name__icontains": "Bat"})
        assert bulk_delete.status_code == 200
        assert bulk_delete.json()["matched"] == 2
        assert client.get("/crud/v1/heroes/v2/json", params={"id": batman["id"]}).status_code == 404
        assert (
            client.get("/crud/v1/heroes/v2/json", params={"id": batgirl["id"]}).status_code == 404
        )
    finally:
        client.delete("/crud/v1/heroes/v2/json", params={"id": batman["id"]})
        client.delete("/crud/v1/heroes/v2/json", params={"id": batgirl["id"]})
        client.delete("/crud/v1/heroes/v2/json", params={"id": superman["id"]})


def test_hero_bulk_actions_with_no_filters_and_no_id_are_rejected() -> None:
    """A bulk PATCH/DELETE with neither id nor filters is rejected, never a full-table action."""
    update_response = client.patch("/crud/v1/heroes/v2/json", json={"name": "Should Not Apply"})
    assert update_response.status_code == 422

    delete_response = client.delete("/crud/v1/heroes/v2/json")
    assert delete_response.status_code == 422


async def test_write_is_committed_before_the_response_is_sent() -> None:
    """A created hero is visible to an independent session by the time POST's body goes out.

    Regression test for the read-after-write race behind tests/e2e's intermittent
    "created hero missing from the list" failures: `get_db` commits in a yield
    dependency's exit code, which at `Depends()`'s default `scope="request"` runs
    *after* the response is sent, so a client that immediately acts on the write's
    own response could reach a database where the INSERT hadn't committed yet.
    `craftainer_crud.models.base.DBSession` pins `scope="function"` to close that window.

    Driven over raw ASGI rather than TestClient because TestClient only returns once
    the whole ASGI cycle (teardown included) has finished, which hides the ordering
    this asserts. The check runs from a separate session, so under the old scope it
    genuinely cannot see the still-uncommitted row.
    """
    name = f"Commit Order {uuid4()}"
    body = json.dumps({"name": name, "powers": ["Durability"]}).encode()
    visible_when_body_sent: list[bool] = []

    async def receive() -> Message:
        """Feed the request body to the app in a single ASGI message."""
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: Message) -> None:
        """Record, as the response body goes out, whether the row is committed yet."""
        if message["type"] != "http.response.body":
            return
        async with async_session_factory() as probe:
            result = await probe.execute(select(Hero).where(Hero.name == name))
            visible_when_body_sent.append(result.scalar_one_or_none() is not None)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.1"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/crud/v1/heroes/v2/json",
            "raw_path": b"/crud/v1/heroes/v2/json",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        },
        receive,
        send,
    )

    try:
        assert visible_when_body_sent == [True], (
            "POST /crud/v1/heroes/v2/json returned its response before the INSERT was committed -- "
            "a client acting on that response can fail to see its own write"
        )
    finally:
        async with async_session_factory() as cleanup:
            await cleanup.execute(delete(Hero).where(Hero.name == name))
            await cleanup.commit()


def test_owner_cannot_update_or_delete_another_owners_hero_against_real_postgres() -> None:
    """Reads stay open across owners, but a second caller can't update/delete Alice's hero.

    Proves craftainer_crud.interfaces.base.OwnerScope(read_scoped=False)'s wiring holds
    end-to-end
    (real get_hero_crud, real SQLAlchemyRepository/session), not just against the
    in-memory fake tests/unit/crud_1/heroes/test_heroes_v2.py and tests/unit/
    interfaces/test_base.py already cover.
    """
    alices_hero = client.post(
        "/crud/v1/heroes/v2/json",
        json={"name": "Owner Isolation Test", "powers": ["Weather control"]},
    ).json()
    assert alices_hero["owner_id"] == "test-user"

    try:
        with _authed_as("bob-integration"):
            get_response = client.get("/crud/v1/heroes/v2/json", params={"id": alices_hero["id"]})
            assert get_response.status_code == 200

            update_response = client.patch(
                "/crud/v1/heroes/v2/json",
                params={"id": alices_hero["id"]},
                json={"powers": ["Hijacked"]},
            )
            assert update_response.status_code == 404

            delete_response = client.delete(
                "/crud/v1/heroes/v2/json", params={"id": alices_hero["id"]}
            )
            assert delete_response.status_code == 404

            bulk_update = client.patch(
                "/crud/v1/heroes/v2/json",
                params={"name__icontains": "Owner Isolation Test"},
                json={"powers": ["Hijacked in bulk"]},
            )
            assert bulk_update.status_code == 200
            assert bulk_update.json()["matched"] == 0

            bulk_delete = client.delete(
                "/crud/v1/heroes/v2/json", params={"name__icontains": "Owner Isolation Test"}
            )
            assert bulk_delete.status_code == 200
            assert bulk_delete.json()["matched"] == 0

        still_alices = client.get("/crud/v1/heroes/v2/json", params={"id": alices_hero["id"]})
        assert still_alices.json()["powers"] == ["Weather control"]
    finally:
        client.delete("/crud/v1/heroes/v2/json", params={"id": alices_hero["id"]})


def test_hero_draft_publish_clone_restore_and_revisions_against_real_postgres() -> None:
    """draft -> publish -> clone -> archive -> restore -> /revisions, over the live app.

    Exercises get_hero_crud's real revisions=RepositoryRevisionSink(...)/actor wiring
    and get_hero_revision_repository (see app.crud_1.heroes.heroes_v2), neither of
    which runs under tests/unit (get_hero_crud is always overridden there).
    """
    draft_response = client.post(
        "/crud/v1/heroes/v2/json/draft", json={"name": "Postgres Draft Hero"}
    )
    assert draft_response.status_code == 201
    hero = draft_response.json()
    assert hero["is_draft"] is True

    hero_ids_to_clean_up = [hero["id"]]
    try:
        missing_publish = client.post("/crud/v1/heroes/v2/json/publish", params={"id": -1})
        assert missing_publish.status_code == 404

        missing_clone = client.post("/crud/v1/heroes/v2/json/clone", params={"id": -1})
        assert missing_clone.status_code == 404

        incomplete_publish = client.post(
            "/crud/v1/heroes/v2/json/publish", params={"id": hero["id"]}
        )
        assert incomplete_publish.status_code == 422

        client.patch("/crud/v1/heroes/v2/json", params={"id": hero["id"]}, json={"powers": ["Ink"]})
        publish_response = client.post("/crud/v1/heroes/v2/json/publish", params={"id": hero["id"]})
        assert publish_response.status_code == 200
        assert publish_response.json()["is_draft"] is False

        clone_response = client.post("/crud/v1/heroes/v2/json/clone", params={"id": hero["id"]})
        assert clone_response.status_code == 201
        clone_id = clone_response.json()["id"]
        hero_ids_to_clean_up.append(clone_id)

        archive_response = client.delete("/crud/v1/heroes/v2/json", params={"id": hero["id"]})
        assert archive_response.status_code == 204

        restore_response = client.post("/crud/v1/heroes/v2/json/restore", params={"id": hero["id"]})
        assert restore_response.status_code == 200

        revisions_response = client.get(
            "/crud/v1/heroes/v2/json/revisions", params={"id": hero["id"]}
        )
        assert revisions_response.status_code == 200
        actions = [revision["action"] for revision in revisions_response.json()]
        assert actions == ["delete", "update", "update", "create"]
    finally:
        for hero_id in hero_ids_to_clean_up:
            client.delete("/crud/v1/heroes/v2/json", params={"id": hero_id})


def test_hero_restore_missing_returns_404_against_real_postgres() -> None:
    """POST /crud/v1/heroes/v2/json/restore?id= for a nonexistent id returns 404."""
    response = client.post("/crud/v1/heroes/v2/json/restore", params={"id": -1})
    assert response.status_code == 404


def test_hero_bulk_restore_via_filters_against_real_postgres() -> None:
    """POST /crud/v1/heroes/v2/json/restore with no id restores every matching archived hero.

    Archive is soft (see craftainer_crud.models.mixins.Archivable), so a plain DELETE in this
    test's own cleanup can't actually remove the row it created -- a fixed name
    filter would keep matching this test's own past runs' now-permanently-archived
    heroes forever. A run-unique name suffix keeps each run's filter scoped to only
    the heroes it itself created.
    """
    suffix = uuid4()
    first = client.post(
        "/crud/v1/heroes/v2/json", json={"name": f"Bulk Restore Test A {suffix}", "powers": ["A"]}
    ).json()
    second = client.post(
        "/crud/v1/heroes/v2/json", json={"name": f"Bulk Restore Test B {suffix}", "powers": ["A"]}
    ).json()
    try:
        client.delete("/crud/v1/heroes/v2/json", params={"id": first["id"]})
        client.delete("/crud/v1/heroes/v2/json", params={"id": second["id"]})

        response = client.post(
            "/crud/v1/heroes/v2/json/restore", params={"name__icontains": str(suffix)}
        )
        assert response.status_code == 200
        assert response.json()["matched"] == 2

        restored = client.get("/crud/v1/heroes/v2/json", params={"name__icontains": str(suffix)})
        assert len(restored.json()) == 2
    finally:
        client.delete("/crud/v1/heroes/v2/json", params={"id": first["id"]})
        client.delete("/crud/v1/heroes/v2/json", params={"id": second["id"]})
