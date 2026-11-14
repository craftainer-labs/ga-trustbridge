"""Unit test: Hero's owner scope comes from the token's `sub`, and a token without one is a 401."""

from collections.abc import Iterator
from typing import Any

import pytest
from craftainer_crud.interfaces.base import OwnerScope
from fastapi import APIRouter, HTTPException
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.config import get_settings
from app.crud_1.heroes.heroes_v2 import get_hero_owner_scope, router
from app.main import app
from app.oidc import get_current_claims


def test_the_scope_is_the_callers_subject_and_only_guards_writes() -> None:
    """`sub` becomes the `owner_id` scope; reads stay open to every authenticated caller."""
    scope = get_hero_owner_scope({"sub": "user-1"})
    assert scope == OwnerScope("owner_id", "user-1", read_scoped=False)


@pytest.mark.parametrize(
    "claims",
    [{}, {"sub": None}, {"sub": ""}, {"sub": "   "}, {"sub": 42}, {"sub": ["a"]}],
)
def test_a_token_without_a_usable_subject_is_refused_with_401(claims: dict[str, Any]) -> None:
    """No `sub` (or a non-string/blank one) never reaches a write as an unowned caller."""
    with pytest.raises(HTTPException) as excinfo:
        get_hero_owner_scope(claims)
    assert excinfo.value.status_code == 401
    assert excinfo.value.headers is not None
    assert 'error="invalid_token"' in excinfo.value.headers["WWW-Authenticate"]


def _routes(router: APIRouter) -> Iterator[APIRoute]:
    """Every route under `router`, descending into the routers it includes."""
    for route in router.routes:
        if isinstance(route, APIRoute):
            yield route
        elif isinstance(inner := getattr(route, "original_router", None), APIRouter):
            yield from _routes(inner)


def _dependency_names(dependant: Dependant) -> set[str]:
    """Names of every callable `dependant` resolves, transitively."""
    names = {getattr(dependant.call, "__name__", "")}
    for sub in dependant.dependencies:
        names |= _dependency_names(sub)
    return names


def test_the_events_routes_resolve_the_scope_not_the_crud_session() -> None:
    """`/events` (JSON and XML) depends on the owner scope, so it holds no session per stream."""
    events = [r for r in _routes(router) if r.path == "/events"]
    assert len(events) == 2
    for route in events:
        names = _dependency_names(route.dependant)
        assert "get_hero_owner_scope" in names
        assert "get_hero_crud" not in names
        assert "get_db" not in names


def test_the_events_route_refuses_a_token_without_a_subject() -> None:
    """A signed token with no `sub` gets 401 from `/events`, not a 500 and not a stream."""
    app.dependency_overrides[get_current_claims] = lambda: {
        "resource_access": {get_settings().oidc_client_id: {"roles": ["viewer"]}}
    }
    try:
        response = TestClient(app).get("/crud/v1/heroes/v2/json/events")
    finally:
        del app.dependency_overrides[get_current_claims]
    assert response.status_code == 401
    assert 'error="invalid_token"' in response.headers["WWW-Authenticate"]
