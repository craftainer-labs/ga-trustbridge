"""Unit test: /crud/v1/heroes/v2/json CRUD routes, with the Hero repository faked out."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from craftainer_crud.interfaces.base import CRUDInterface, OwnerScope, RepositoryRevisionSink
from craftainer_crud.models.revision import Revision
from craftainer_crud.repositories.memory import InMemoryRepository
from fastapi import Depends
from fastapi.testclient import TestClient

from app.config import get_settings
from app.crud_1.heroes.heroes_v2 import get_hero_crud, get_hero_revision_repository
from app.main import app
from app.models.hero import Hero as HeroModel
from app.oidc import get_current_claims
from app.views.hero_v2 import HeroV2

from .conftest import override_hero_crud as _override_crud

client = TestClient(app)


@contextmanager
def _authed_as(sub: str) -> Iterator[None]:
    """Override get_current_claims to authenticate as `sub`, with every RBAC role.

    Restores whatever override was in place on exit, so this can nest inside the
    `authed` fixture (tests/unit/conftest.py) without clobbering it.
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


def test_hero_crud_lifecycle(authed: None) -> None:
    """Create, list, get, update, and delete a hero through the HTTP routes."""
    # One repository instance shared across every request in this test -- FastAPI
    # calls the override afresh per request, so a per-call repository would silently
    # discard state between requests.
    repository = InMemoryRepository(HeroModel)
    app.dependency_overrides[get_hero_crud] = _override_crud(repository)
    try:
        create_response = client.post(
            "/crud/v1/heroes/v2/json", json={"name": "Spider-Man", "powers": ["Wall-crawling"]}
        )
        assert create_response.status_code == 201
        hero_id = create_response.json()["id"]

        list_response = client.get("/crud/v1/heroes/v2/json")
        assert list_response.status_code == 200
        assert len(list_response.json()) == 1

        get_response = client.get("/crud/v1/heroes/v2/json", params={"id": hero_id})
        assert get_response.status_code == 200
        assert get_response.json()["name"] == "Spider-Man"

        update_response = client.patch(
            "/crud/v1/heroes/v2/json", params={"id": hero_id}, json={"powers": ["Web-slinging"]}
        )
        assert update_response.status_code == 200
        assert update_response.json()["powers"] == ["Web-slinging"]

        delete_response = client.delete("/crud/v1/heroes/v2/json", params={"id": hero_id})
        assert delete_response.status_code == 204

        missing_response = client.get("/crud/v1/heroes/v2/json", params={"id": hero_id})
        assert missing_response.status_code == 404
    finally:
        del app.dependency_overrides[get_hero_crud]


def test_get_missing_hero_returns_404(authed: None) -> None:
    """GET /crud/v1/heroes/v2/json?id= for a nonexistent id returns 404."""
    app.dependency_overrides[get_hero_crud] = _override_crud(InMemoryRepository(HeroModel))
    try:
        response = client.get("/crud/v1/heroes/v2/json", params={"id": 999})
    finally:
        del app.dependency_overrides[get_hero_crud]
    assert response.status_code == 404


def test_update_missing_hero_returns_404(authed: None) -> None:
    """PATCH /crud/v1/heroes/v2/json?id= for a nonexistent id returns 404."""
    app.dependency_overrides[get_hero_crud] = _override_crud(InMemoryRepository(HeroModel))
    try:
        response = client.patch(
            "/crud/v1/heroes/v2/json", params={"id": 999}, json={"name": "Nobody"}
        )
    finally:
        del app.dependency_overrides[get_hero_crud]
    assert response.status_code == 404


def test_delete_missing_hero_returns_404(authed: None) -> None:
    """DELETE /crud/v1/heroes/v2/json?id= for a nonexistent id returns 404."""
    app.dependency_overrides[get_hero_crud] = _override_crud(InMemoryRepository(HeroModel))
    try:
        response = client.delete("/crud/v1/heroes/v2/json", params={"id": 999})
    finally:
        del app.dependency_overrides[get_hero_crud]
    assert response.status_code == 404


def test_hero_routes_require_auth() -> None:
    """GET /crud/v1/heroes/v2/json with no Authorization header is rejected with 401."""
    response = client.get("/crud/v1/heroes/v2/json")
    assert response.status_code == 401


def test_hero_create_requires_write_role() -> None:
    """POST /crud/v1/heroes/v2/json with only the viewer role is rejected with 403."""
    settings = get_settings()
    app.dependency_overrides[get_current_claims] = lambda: {
        "sub": "viewer-user",
        "resource_access": {settings.oidc_client_id: {"roles": ["viewer"]}},
    }
    try:
        response = client.post("/crud/v1/heroes/v2/json", json={"name": "X", "powers": ["Y"]})
    finally:
        del app.dependency_overrides[get_current_claims]
    assert response.status_code == 403


# --- Ownership: reads open to everyone, writes restricted to the creator -----


def test_hero_create_stamps_owner_id_from_claims(authed: None) -> None:
    """create() stamps owner_id from the caller's own claims, ignoring any client input."""
    app.dependency_overrides[get_hero_crud] = _override_crud(InMemoryRepository(HeroModel))
    try:
        response = client.post(
            "/crud/v1/heroes/v2/json", json={"name": "Spider-Man", "powers": ["Wall-crawling"]}
        )
    finally:
        del app.dependency_overrides[get_hero_crud]
    assert response.json()["owner_id"] == "test-user"


def test_any_caller_can_list_and_get_another_owners_hero(authed: None) -> None:
    """Bob can list/get a hero Alice ("test-user") created -- reads are open to everyone."""
    repository = InMemoryRepository(HeroModel)
    app.dependency_overrides[get_hero_crud] = _override_crud(repository)
    try:
        alices_hero = client.post(
            "/crud/v1/heroes/v2/json", json={"name": "Storm", "powers": ["Weather control"]}
        ).json()

        with _authed_as("bob"):
            list_response = client.get("/crud/v1/heroes/v2/json")
            assert any(hero["id"] == alices_hero["id"] for hero in list_response.json())

            get_response = client.get("/crud/v1/heroes/v2/json", params={"id": alices_hero["id"]})
            assert get_response.status_code == 200
    finally:
        del app.dependency_overrides[get_hero_crud]


def test_caller_cannot_update_another_owners_hero(authed: None) -> None:
    """Bob's PATCH by id 404s (and makes no change) for a hero Alice created."""
    repository = InMemoryRepository(HeroModel)
    app.dependency_overrides[get_hero_crud] = _override_crud(repository)
    try:
        alices_hero = client.post(
            "/crud/v1/heroes/v2/json", json={"name": "Storm", "powers": ["Weather control"]}
        ).json()

        with _authed_as("bob"):
            response = client.patch(
                "/crud/v1/heroes/v2/json",
                params={"id": alices_hero["id"]},
                json={"powers": ["Hijacked"]},
            )
            assert response.status_code == 404

        unchanged = client.get("/crud/v1/heroes/v2/json", params={"id": alices_hero["id"]})
        assert unchanged.json()["powers"] == ["Weather control"]
    finally:
        del app.dependency_overrides[get_hero_crud]


def test_caller_cannot_delete_another_owners_hero(authed: None) -> None:
    """Bob's DELETE by id 404s (and deletes nothing) for a hero Alice created."""
    repository = InMemoryRepository(HeroModel)
    app.dependency_overrides[get_hero_crud] = _override_crud(repository)
    try:
        alices_hero = client.post(
            "/crud/v1/heroes/v2/json", json={"name": "Storm", "powers": ["Weather control"]}
        ).json()

        with _authed_as("bob"):
            response = client.delete("/crud/v1/heroes/v2/json", params={"id": alices_hero["id"]})
            assert response.status_code == 404

        still_there = client.get("/crud/v1/heroes/v2/json", params={"id": alices_hero["id"]})
        assert still_there.status_code == 200
    finally:
        del app.dependency_overrides[get_hero_crud]


def test_caller_cannot_publish_another_owners_draft(authed: None) -> None:
    """Bob's POST /publish?id= 404s for a draft Alice created, rather than 500ing.

    `crud.get` (unscoped, see OwnerScope's read_scoped=False) lets Bob see Alice's
    draft exists, but `crud.update` (always owner-scoped) returns None since he
    doesn't own it -- craftainer_crud.controllers.crud_router's publish_record must turn that
    into a 404, not return None as its `-> schema` response.
    """
    repository = InMemoryRepository(HeroModel)
    app.dependency_overrides[get_hero_crud] = _override_crud(repository)
    try:
        alices_draft = client.post(
            "/crud/v1/heroes/v2/json/draft", json={"name": "Nightwing"}
        ).json()
        client.patch(
            "/crud/v1/heroes/v2/json",
            params={"id": alices_draft["id"]},
            json={"powers": ["Acrobatics"]},
        )

        with _authed_as("bob"):
            response = client.post(
                "/crud/v1/heroes/v2/json/publish", params={"id": alices_draft["id"]}
            )
            assert response.status_code == 404
    finally:
        del app.dependency_overrides[get_hero_crud]


def test_xml_caller_cannot_publish_another_owners_draft(authed: None) -> None:
    """The same 404-not-500 guard (test_caller_cannot_publish_another_owners_draft)
    also applies through the XML router's publish_record_xml.
    """
    repository = InMemoryRepository(HeroModel)
    app.dependency_overrides[get_hero_crud] = _override_crud(repository)
    try:
        create_response = client.post(
            "/crud/v1/heroes/v2/xml/draft",
            content="<hero><name>Nightwing</name></hero>",
            headers={"Content-Type": "application/xml"},
        )
        hero_id = create_response.text.split("<id>")[1].split("</id>")[0]
        client.patch(
            "/crud/v1/heroes/v2/xml",
            params={"id": hero_id},
            content="<hero><powers>Acrobatics</powers></hero>",
            headers={"Content-Type": "application/xml"},
        )

        with _authed_as("bob"):
            response = client.post("/crud/v1/heroes/v2/xml/publish", params={"id": hero_id})
            assert response.status_code == 404

        still_draft = client.get("/crud/v1/heroes/v2/json", params={"id": hero_id}).json()
        assert still_draft["is_draft"] is True
    finally:
        del app.dependency_overrides[get_hero_crud]


def test_caller_bulk_update_and_delete_do_not_reach_another_owners_hero(authed: None) -> None:
    """Bob's bulk PATCH/DELETE, filtered broadly, matches none of Alice's heroes."""
    repository = InMemoryRepository(HeroModel)
    app.dependency_overrides[get_hero_crud] = _override_crud(repository)
    try:
        client.post(
            "/crud/v1/heroes/v2/json",
            json={"name": "Shared Name Test", "powers": ["Weather control"]},
        )

        with _authed_as("bob"):
            bulk_update = client.patch(
                "/crud/v1/heroes/v2/json",
                params={"name__icontains": "Shared Name Test"},
                json={"powers": ["Hijacked"]},
            )
            assert bulk_update.status_code == 200
            assert bulk_update.json()["matched"] == 0

            bulk_delete = client.delete(
                "/crud/v1/heroes/v2/json", params={"name__icontains": "Shared Name Test"}
            )
            assert bulk_delete.status_code == 200
            assert bulk_delete.json()["matched"] == 0

        # Still there and unchanged -- Bob's bulk actions matched nothing of Alice's.
        still_there = client.get(
            "/crud/v1/heroes/v2/json", params={"name__icontains": "Shared Name Test"}
        )
        assert len(still_there.json()) == 1
        assert still_there.json()[0]["powers"] == ["Weather control"]
    finally:
        del app.dependency_overrides[get_hero_crud]


# --- Record-lifecycle mixins: archive/draft/publish/lock/clone/revisions -----


def _override_crud_with_revisions(
    repository: InMemoryRepository[HeroModel], revision_repository: InMemoryRepository[Revision]
) -> Any:  # noqa: ANN401 -- a FastAPI dependency-override callable, shape checked by the framework
    """Build a get_hero_crud override wiring RepositoryRevisionSink, mirroring the real
    get_hero_crud's own revisions=RepositoryRevisionSink(...) wiring (see
    app.crud_1.heroes.heroes_v2) -- only the storage is faked.
    """

    def _build(
        claims: Annotated[dict[str, Any], Depends(get_current_claims)],
    ) -> CRUDInterface[HeroV2, HeroModel]:
        return CRUDInterface(
            schema=HeroV2,
            repository=repository,
            owner=OwnerScope("owner_id", claims["sub"], read_scoped=False),
            revisions=RepositoryRevisionSink(revision_repository),
            resource="hero",
            actor=str(claims.get("sub", "unknown")),
        )

    return _build


def test_hero_record_lifecycle_draft_through_revisions(authed: None) -> None:
    """The full opt-in record-lifecycle sequence: draft -> publish -> lock -> attempt
    (and fail) an edit -> unlock -> archive -> list (excluded) -> list with
    include_archived -> restore -> clone -> /revisions reflects the sequence.
    """
    repository = InMemoryRepository(HeroModel)
    revision_repository = InMemoryRepository(Revision)
    app.dependency_overrides[get_hero_crud] = _override_crud_with_revisions(
        repository, revision_repository
    )
    app.dependency_overrides[get_hero_revision_repository] = lambda: revision_repository
    try:
        draft_response = client.post("/crud/v1/heroes/v2/json/draft", json={"name": "Nightwing"})
        assert draft_response.status_code == 201
        draft = draft_response.json()
        assert draft["is_draft"] is True
        assert draft["powers"] is None
        hero_id = draft["id"]

        incomplete_publish = client.post("/crud/v1/heroes/v2/json/publish", params={"id": hero_id})
        assert incomplete_publish.status_code == 422

        complete_response = client.patch(
            "/crud/v1/heroes/v2/json", params={"id": hero_id}, json={"powers": ["Acrobatics"]}
        )
        assert complete_response.status_code == 200

        publish_response = client.post("/crud/v1/heroes/v2/json/publish", params={"id": hero_id})
        assert publish_response.status_code == 200
        assert publish_response.json()["is_draft"] is False

        lock_response = client.patch(
            "/crud/v1/heroes/v2/json", params={"id": hero_id}, json={"is_locked": True}
        )
        assert lock_response.status_code == 200
        assert lock_response.json()["is_locked"] is True

        failed_edit = client.patch(
            "/crud/v1/heroes/v2/json",
            params={"id": hero_id},
            json={"powers": ["Should not apply"]},
        )
        assert failed_edit.status_code == 423

        unlock_response = client.patch(
            "/crud/v1/heroes/v2/json", params={"id": hero_id}, json={"is_locked": False}
        )
        assert unlock_response.status_code == 200
        assert unlock_response.json()["is_locked"] is False

        archive_response = client.delete("/crud/v1/heroes/v2/json", params={"id": hero_id})
        assert archive_response.status_code == 204

        excluded_response = client.get("/crud/v1/heroes/v2/json", params={"id": hero_id})
        assert excluded_response.status_code == 404

        included_response = client.get(
            "/crud/v1/heroes/v2/json", params={"id": hero_id, "include_archived": "true"}
        )
        assert included_response.status_code == 200
        assert included_response.json()["archived_at"] is not None

        restore_response = client.post("/crud/v1/heroes/v2/json/restore", params={"id": hero_id})
        assert restore_response.status_code == 200
        assert restore_response.json()["archived_at"] is None

        clone_response = client.post("/crud/v1/heroes/v2/json/clone", params={"id": hero_id})
        assert clone_response.status_code == 201
        clone = clone_response.json()
        assert clone["id"] != hero_id
        assert clone["name"] == "Nightwing"
        assert clone["powers"] == ["Acrobatics"]
        # A clone of a Draftable record is always created as a draft, regardless of the
        # source's own (now-published) state -- see crud_router.py's clone_record.
        assert clone["is_draft"] is True

        revisions_response = client.get("/crud/v1/heroes/v2/json/revisions", params={"id": hero_id})
        assert revisions_response.status_code == 200
        revisions = revisions_response.json()
        # Newest first; the clone's own "create" is filed under *its* id, not
        # hero_id (see crud_router.py's clone_record), restore() isn't itself
        # revision-logged (see craftainer_crud.interfaces.base.CRUDInterface.restore), and the
        # locked edit above never reached a mutation -- so this hero's own log is
        # just its own create plus every update/delete that actually applied.
        assert [revision["action"] for revision in revisions] == [
            "delete",
            "update",
            "update",
            "update",
            "update",
            "create",
        ]
        assert all(revision["record_id"] == hero_id for revision in revisions)
    finally:
        del app.dependency_overrides[get_hero_crud]
        del app.dependency_overrides[get_hero_revision_repository]


def test_hero_bulk_restore_via_filters(authed: None) -> None:
    """POST /crud/v1/heroes/v2/json/restore with no id restores every matching archived hero."""
    repository = InMemoryRepository(HeroModel)
    app.dependency_overrides[get_hero_crud] = _override_crud(repository)
    try:
        first = client.post(
            "/crud/v1/heroes/v2/json", json={"name": "Robin", "powers": ["Acrobatics"]}
        ).json()
        second = client.post(
            "/crud/v1/heroes/v2/json", json={"name": "Robinhood", "powers": ["Archery"]}
        ).json()
        client.delete("/crud/v1/heroes/v2/json", params={"id": first["id"]})
        client.delete("/crud/v1/heroes/v2/json", params={"id": second["id"]})

        response = client.post(
            "/crud/v1/heroes/v2/json/restore", params={"name__icontains": "Robin"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["matched"] == 2
        assert {first["id"], second["id"]} == set(body["ids"])

        restored = client.get("/crud/v1/heroes/v2/json", params={"name__icontains": "Robin"})
        assert len(restored.json()) == 2
    finally:
        del app.dependency_overrides[get_hero_crud]


def test_hero_restore_missing_returns_404(authed: None) -> None:
    """POST /crud/v1/heroes/v2/json/restore?id= for a nonexistent id returns 404."""
    app.dependency_overrides[get_hero_crud] = _override_crud(InMemoryRepository(HeroModel))
    try:
        response = client.post("/crud/v1/heroes/v2/json/restore", params={"id": 999})
    finally:
        del app.dependency_overrides[get_hero_crud]
    assert response.status_code == 404


def test_hero_bulk_lock_blocks_bulk_update_and_delete(authed: None) -> None:
    """A locked hero is skipped by neither -- bulk PATCH/DELETE against it 423s, matching
    single-record lock enforcement, since Hero's OwnerScope routes even single-record
    update/delete through the repository's update_many/delete_many (see
    craftainer_crud.interfaces.base.OwnerScope's own docstring).
    """
    repository = InMemoryRepository(HeroModel)
    app.dependency_overrides[get_hero_crud] = _override_crud(repository)
    try:
        hero = client.post(
            "/crud/v1/heroes/v2/json", json={"name": "Locked Hero", "powers": ["Immovable"]}
        ).json()
        client.patch("/crud/v1/heroes/v2/json", params={"id": hero["id"]}, json={"is_locked": True})

        bulk_update = client.patch(
            "/crud/v1/heroes/v2/json",
            params={"name__icontains": "Locked Hero"},
            json={"powers": ["Should not apply"]},
        )
        assert bulk_update.status_code == 423

        bulk_delete = client.delete(
            "/crud/v1/heroes/v2/json", params={"name__icontains": "Locked Hero"}
        )
        assert bulk_delete.status_code == 423

        still_there = client.get("/crud/v1/heroes/v2/json", params={"id": hero["id"]})
        assert still_there.json()["powers"] == ["Immovable"]
    finally:
        del app.dependency_overrides[get_hero_crud]


def test_hero_delete_by_id_while_locked_returns_423(authed: None) -> None:
    """DELETE /crud/v1/heroes/v2/json?id= for a locked hero 423s (and deletes nothing)."""
    repository = InMemoryRepository(HeroModel)
    app.dependency_overrides[get_hero_crud] = _override_crud(repository)
    try:
        hero = client.post(
            "/crud/v1/heroes/v2/json", json={"name": "Locked Hero", "powers": ["Immovable"]}
        ).json()
        client.patch("/crud/v1/heroes/v2/json", params={"id": hero["id"]}, json={"is_locked": True})

        response = client.delete("/crud/v1/heroes/v2/json", params={"id": hero["id"]})
        assert response.status_code == 423

        still_there = client.get("/crud/v1/heroes/v2/json", params={"id": hero["id"]})
        assert still_there.status_code == 200
    finally:
        del app.dependency_overrides[get_hero_crud]


def test_hero_bulk_restore_with_no_filters_and_no_id_rejected(authed: None) -> None:
    """POST /crud/v1/heroes/v2/json/restore with neither id nor filters is rejected (422)."""
    app.dependency_overrides[get_hero_crud] = _override_crud(InMemoryRepository(HeroModel))
    try:
        response = client.post("/crud/v1/heroes/v2/json/restore")
    finally:
        del app.dependency_overrides[get_hero_crud]
    assert response.status_code == 422


# --- XML parity: restore/draft/publish/revisions/clone -----------------------


def test_hero_xml_router_lifecycle_parity(authed: None) -> None:
    """The same draft -> publish -> archive -> restore -> clone -> /revisions sequence
    the JSON router demonstrates (test_hero_record_lifecycle_draft_through_revisions)
    also works through the XML router, added for parity by this plan.
    """
    repository = InMemoryRepository(HeroModel)
    revision_repository = InMemoryRepository(Revision)
    app.dependency_overrides[get_hero_crud] = _override_crud_with_revisions(
        repository, revision_repository
    )
    app.dependency_overrides[get_hero_revision_repository] = lambda: revision_repository
    try:
        draft_response = client.post(
            "/crud/v1/heroes/v2/xml/draft",
            content="<hero><name>Batgirl</name></hero>",
            headers={"Content-Type": "application/xml"},
        )
        assert draft_response.status_code == 201
        assert "<is_draft>True</is_draft>" in draft_response.text
        hero_id = draft_response.text.split("<id>")[1].split("</id>")[0]

        incomplete_publish = client.post("/crud/v1/heroes/v2/xml/publish", params={"id": hero_id})
        assert incomplete_publish.status_code == 422

        client.patch(
            "/crud/v1/heroes/v2/xml",
            params={"id": hero_id},
            content="<hero><powers>Acrobatics</powers></hero>",
            headers={"Content-Type": "application/xml"},
        )

        publish_response = client.post("/crud/v1/heroes/v2/xml/publish", params={"id": hero_id})
        assert publish_response.status_code == 200
        assert "<is_draft>False</is_draft>" in publish_response.text

        archive_response = client.delete("/crud/v1/heroes/v2/xml", params={"id": hero_id})
        assert archive_response.status_code == 204

        restore_response = client.post("/crud/v1/heroes/v2/xml/restore", params={"id": hero_id})
        assert restore_response.status_code == 200
        # A None field is omitted entirely, not rendered as literal "None" text --
        # see craftainer_crud.xml_codec.to_xml's own docstring.
        assert "<archived_at>" not in restore_response.text

        clone_response = client.post("/crud/v1/heroes/v2/xml/clone", params={"id": hero_id})
        assert clone_response.status_code == 201
        assert "<name>Batgirl</name>" in clone_response.text

        revisions_response = client.get("/crud/v1/heroes/v2/xml/revisions", params={"id": hero_id})
        assert revisions_response.status_code == 200
        assert revisions_response.headers["content-type"] == "application/xml"
        assert "<revisions>" in revisions_response.text
        assert revisions_response.text.count("<revision>") >= 2
    finally:
        del app.dependency_overrides[get_hero_crud]
        del app.dependency_overrides[get_hero_revision_repository]


def test_hero_xml_bulk_restore_with_no_filters_and_no_id_rejected(authed: None) -> None:
    """POST /crud/v1/heroes/v2/xml/restore with neither id nor filters is rejected (422)."""
    app.dependency_overrides[get_hero_crud] = _override_crud(InMemoryRepository(HeroModel))
    try:
        response = client.post("/crud/v1/heroes/v2/xml/restore")
    finally:
        del app.dependency_overrides[get_hero_crud]
    assert response.status_code == 422


# --- Statistics/predictions: JSON + XML ---------------------------------------


async def _seed_heroes_across_three_days(repository: InMemoryRepository[HeroModel]) -> list[int]:
    """Create 3 heroes and backdate `created_at` onto 3 distinct UTC calendar days.

    InMemoryRepository.create always stamps `created_at`/`updated_at` itself (see
    its own module docstring), so a caller can't set it through the normal create
    path -- this reaches into the fake repository's storage directly afterwards,
    same as InMemoryRepository's own internal `_records` dict, purely to make
    `/stats?bucket=day`'s time series and `/predict`'s forecast deterministic
    instead of depending on wall-clock timing.
    """
    ids = []
    for index in range(3):
        hero = await repository.create(
            {
                "name": f"Hero {index}",
                "powers": ["A"],
                "owner_id": "tester",
            }
        )
        base = datetime.now(UTC).replace(tzinfo=None, hour=12, minute=0, second=0, microsecond=0)
        repository._records[hero.id].created_at = base - timedelta(days=2 - index)
        ids.append(hero.id)
    return ids


async def test_hero_stats_json(authed: None) -> None:
    """GET /crud/v1/heroes/v2/json/stats reports count/numeric/categorical/time-series/lifecycle."""
    repository = InMemoryRepository(HeroModel)
    app.dependency_overrides[get_hero_crud] = _override_crud(repository)
    try:
        await _seed_heroes_across_three_days(repository)
        response = client.get("/crud/v1/heroes/v2/json/stats", params={"bucket": "day"})
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 3
        numeric_fields = {item["field"] for item in body["numeric"]}
        assert "id" in numeric_fields
        categorical_fields = {item["field"] for item in body["categorical"]}
        assert {"is_draft", "is_locked"}.issubset(categorical_fields)
        assert len(body["time_series"]) == 3
        assert body["lifecycle"]["archived"] == 0
    finally:
        del app.dependency_overrides[get_hero_crud]


async def test_hero_stats_without_bucket_omits_time_series(authed: None) -> None:
    """GET /stats with no `?bucket=` omits the time series entirely."""
    repository = InMemoryRepository(HeroModel)
    app.dependency_overrides[get_hero_crud] = _override_crud(repository)
    try:
        await _seed_heroes_across_three_days(repository)
        response = client.get("/crud/v1/heroes/v2/json/stats")
        assert response.status_code == 200
        assert response.json()["time_series"] is None
    finally:
        del app.dependency_overrides[get_hero_crud]


async def test_hero_predict_record_count_json(authed: None) -> None:
    """GET /crud/v1/heroes/v2/json/predict forecasts future buckets via linear regression."""
    repository = InMemoryRepository(HeroModel)
    app.dependency_overrides[get_hero_crud] = _override_crud(repository)
    try:
        await _seed_heroes_across_three_days(repository)
        response = client.get(
            "/crud/v1/heroes/v2/json/predict", params={"periods": 2, "bucket": "day"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["field"] is None
        assert body["method"] == "linear_regression"
        assert body["bucket"] == "day"
        assert len(body["predictions"]) == 2
    finally:
        del app.dependency_overrides[get_hero_crud]


async def test_hero_predict_numeric_field_json(authed: None) -> None:
    """GET /predict?field=id forecasts that numeric field's per-bucket sum instead of count."""
    repository = InMemoryRepository(HeroModel)
    app.dependency_overrides[get_hero_crud] = _override_crud(repository)
    try:
        await _seed_heroes_across_three_days(repository)
        response = client.get(
            "/crud/v1/heroes/v2/json/predict", params={"field": "id", "periods": 1, "bucket": "day"}
        )
        assert response.status_code == 200
        assert response.json()["field"] == "id"
    finally:
        del app.dependency_overrides[get_hero_crud]


async def test_hero_predict_unrecognized_field_returns_422(authed: None) -> None:
    """GET /predict?field= for a non-numeric (or unknown) field is rejected (422)."""
    repository = InMemoryRepository(HeroModel)
    app.dependency_overrides[get_hero_crud] = _override_crud(repository)
    try:
        await _seed_heroes_across_three_days(repository)
        response = client.get(
            "/crud/v1/heroes/v2/json/predict", params={"field": "name", "bucket": "day"}
        )
    finally:
        del app.dependency_overrides[get_hero_crud]
    assert response.status_code == 422


def test_hero_predict_insufficient_history_returns_422(authed: None) -> None:
    """GET /predict with fewer than 2 time buckets of history is rejected (422)."""
    repository = InMemoryRepository(HeroModel)
    app.dependency_overrides[get_hero_crud] = _override_crud(repository)
    try:
        client.post("/crud/v1/heroes/v2/json", json={"name": "Solo", "powers": ["A"]})
        response = client.get("/crud/v1/heroes/v2/json/predict", params={"bucket": "day"})
    finally:
        del app.dependency_overrides[get_hero_crud]
    assert response.status_code == 422


def test_hero_predict_invalid_bucket_returns_422(authed: None) -> None:
    """GET /predict?bucket=<invalid> is rejected (422), not a 500."""
    repository = InMemoryRepository(HeroModel)
    app.dependency_overrides[get_hero_crud] = _override_crud(repository)
    try:
        response = client.get("/crud/v1/heroes/v2/json/predict", params={"bucket": "fortnight"})
    finally:
        del app.dependency_overrides[get_hero_crud]
    assert response.status_code == 422


async def test_hero_stats_and_predict_xml(authed: None) -> None:
    """GET /crud/v1/heroes/v2/xml/stats and /predict hand-assemble the same data as nested XML."""
    repository = InMemoryRepository(HeroModel)
    app.dependency_overrides[get_hero_crud] = _override_crud(repository)
    try:
        await _seed_heroes_across_three_days(repository)

        stats_response = client.get("/crud/v1/heroes/v2/xml/stats", params={"bucket": "day"})
        assert stats_response.status_code == 200
        assert stats_response.headers["content-type"] == "application/xml"
        assert "<stats>" in stats_response.text
        assert "<total>3</total>" in stats_response.text
        assert "<numeric-fields>" in stats_response.text
        assert "<categorical-values>" in stats_response.text
        assert "<time-buckets>" in stats_response.text
        assert "<lifecycle>" in stats_response.text

        predict_response = client.get(
            "/crud/v1/heroes/v2/xml/predict", params={"periods": 2, "bucket": "day"}
        )
        assert predict_response.status_code == 200
        assert predict_response.headers["content-type"] == "application/xml"
        assert "<prediction>" in predict_response.text
        assert "<meta>" in predict_response.text
        assert "<method>linear_regression</method>" in predict_response.text
        assert predict_response.text.count("<prediction-point>") == 2
    finally:
        del app.dependency_overrides[get_hero_crud]
