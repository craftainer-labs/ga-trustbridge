"""Unit test: GET /api/_crud/manifest and GET /.well-known/api-catalog on the real app."""

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest
from craftainer_crud.controllers.crud_manifest import crud_manifest_router
from craftainer_crud.manifest import MANIFEST_PROFILE, ManifestRegistry
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.crud_1.wiring import manifest_registry
from app.main import _api_catalog_router, app
from app.oidc import get_current_claims

client = TestClient(app)
_MANIFEST = "/api/_crud/manifest"


def _claims_as(*roles: str) -> Callable[[], dict[str, Any]]:
    settings = get_settings()
    return lambda: {
        "sub": "test-user",
        "resource_access": {settings.oidc_client_id: {"roles": list(roles)}},
    }


def test_api_catalog_points_at_the_manifest_without_authentication() -> None:
    """The linkset is served to anyone, as application/linkset+json, with the profile."""
    response = client.get("/.well-known/api-catalog")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/linkset+json")
    entry = response.json()["linkset"][0]
    assert entry["anchor"] == "http://testserver"
    assert entry["service-desc"][0]["href"] == "/openapi.json"
    assert entry["describedby"] == [
        {"href": _MANIFEST, "type": "application/json", "profile": MANIFEST_PROFILE}
    ]


@pytest.mark.parametrize(("mode", "advertised"), [("dev", True), ("production", False)])
def test_api_catalog_advertises_openapi_only_where_it_is_served(
    mode: str, advertised: bool
) -> None:
    """Production serves no /openapi.json, so its catalog carries no service-desc link."""
    catalog_app = FastAPI()
    catalog_app.include_router(_api_catalog_router(mode))

    entry = TestClient(catalog_app).get("/.well-known/api-catalog").json()["linkset"][0]

    assert ("service-desc" in entry) is advertised


def test_anonymous_caller_sees_no_resource_because_none_is_public() -> None:
    """Hero requires a role to read, so an unauthenticated manifest lists nothing."""
    response = client.get(_MANIFEST)

    assert response.status_code == 200
    body = response.json()
    assert body["manifest_version"] == "1"
    assert body["service"]["name"] == get_settings().app_name
    assert body["resources"] == []


@pytest.mark.parametrize("role", ["viewer", "editor", "maintainer", "detective"])
def test_a_reading_role_sees_both_hero_versions(role: str) -> None:
    """Each role that may read heroes sees v1 and v2, with the role names of the router."""
    app.dependency_overrides[get_current_claims] = _claims_as(role)
    try:
        response = client.get(_MANIFEST)
    finally:
        del app.dependency_overrides[get_current_claims]

    assert response.status_code == 200
    resources = {(r["name"], r["api_version"]): r for r in response.json()["resources"]}
    assert set(resources) == {("heroes", "v1"), ("heroes", "v2")}
    v2 = resources[("heroes", "v2")]
    assert v2["base_path"] == "/crud/v1/heroes/v2/json"
    assert v2["auth"]["read_roles"] == ["viewer", "editor", "maintainer", "detective"]
    assert v2["auth"]["write_roles"] == ["editor", "maintainer"]
    assert v2["auth"]["delete_roles"] == ["maintainer"]


def test_a_role_that_cannot_read_heroes_sees_none() -> None:
    """A valid token without a hero-reading role is refused per resource, not for the route."""
    app.dependency_overrides[get_current_claims] = _claims_as("security")
    try:
        response = client.get(_MANIFEST)
    finally:
        del app.dependency_overrides[get_current_claims]

    assert response.status_code == 200
    assert response.json()["resources"] == []


def test_manifest_etag_cache_control_and_not_modified() -> None:
    """The library's weak ETag and revalidation Cache-Control; a matching tag gets a 304."""
    app.dependency_overrides[get_current_claims] = _claims_as("viewer")
    try:
        first = client.get(_MANIFEST)
        etag = first.headers["etag"]
        revalidated = client.get(_MANIFEST, headers={"If-None-Match": etag})
    finally:
        del app.dependency_overrides[get_current_claims]

    assert etag.startswith('W/"')
    assert first.headers["cache-control"] == "private, no-cache"
    assert revalidated.status_code == 304
    assert revalidated.content == b""
    assert revalidated.headers["etag"] == etag


def test_manifest_etag_differs_between_callers_who_see_different_resources() -> None:
    """The tag covers the caller-filtered document, so anonymous and viewer never share one."""
    anonymous = client.get(_MANIFEST).headers["etag"]
    app.dependency_overrides[get_current_claims] = _claims_as("viewer")
    try:
        viewer = client.get(_MANIFEST).headers["etag"]
        stale = client.get(_MANIFEST, headers={"If-None-Match": anonymous})
    finally:
        del app.dependency_overrides[get_current_claims]

    assert anonymous != viewer
    assert stale.status_code == 200


def test_a_public_read_resource_is_listed_for_anonymous_callers() -> None:
    """A registration marked public_read is visible without a token; the others still are not."""
    hero_v2 = next(r for r in manifest_registry.resources() if r.api_version == "v2")
    registry = ManifestRegistry()
    registry.register(replace(hero_v2, name="open_heroes", public_read=True, read_roles=()))
    registry.register(replace(hero_v2, name="private_heroes"))
    local = FastAPI()
    local.include_router(
        crud_manifest_router(registry, service_name="t", service_version="1", prefix=""),
        prefix="/api/_crud",
    )

    response = TestClient(local).get(_MANIFEST)

    assert [r["name"] for r in response.json()["resources"]] == ["open_heroes"]
