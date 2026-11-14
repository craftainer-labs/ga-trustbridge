"""HTTP routes for the current (v2) Hero resource -- a worked example of the generic CRUD interface.

One `build_resource_router` call builds the JSON/XML/web sibling routes
together. `prefix=""` here deliberately: this router carries none of its
own mount prefix -- `app.crud_1.heroes`'s `__init__.py` is the one that
assigns `/v2` explicitly, via `include_router(router, prefix=...)`,
when it combines this with the deprecated `heroes_v1.py` sibling into the
one `router` `app.crud_1` mounts. See `crud_1/README.md`'s "Don't" section
for why a resource-version router should never bake in its own prefix.
`api_prefix` is still the full absolute path, though -- see
`crud_router.py`'s "Generic CRUD router factories" and `docs/adrs/0009-...md`.
"""

from collections.abc import Mapping
from typing import Annotated, Any

from craftainer_crud.controllers.crud_router import ROUTER_VERSION, build_resource_router
from craftainer_crud.interfaces.base import (
    CRUDInterface,
    EventSource,
    OwnerScope,
    RepositoryRevisionSink,
)
from craftainer_crud.interfaces.dependency import (
    build_event_sink_provider,
    build_event_source_provider,
    build_repository_provider,
)
from craftainer_crud.manifest import ManifestOptions
from craftainer_crud.models.base import DBSession
from craftainer_crud.models.revision import Revision
from craftainer_crud.repositories.base import Repository
from fastapi import Depends

from app.crud_1.analytics_events import AnalyticsEventSink
from app.crud_1.wiring import bulk_rate_limit, manifest_registry, stats_cache
from app.models.hero import Hero as HeroModel
from app.oidc import get_current_claims, require_roles, subject_of
from app.views.hero_v2 import HeroV2, HeroV2Create, HeroV2Update

_hero_repository = build_repository_provider(HeroModel)
_revision_repository = build_repository_provider(Revision)
_hero_event_sink = build_event_sink_provider("hero")
_hero_event_source = build_event_source_provider("hero")


def _hero_analytics_props(_action: str, snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    """The analytics props of a hero event: only how many powers, never a name or owner.

    `powers_count` must be on `ANALYTICS_PROP_KEYS` to be sent (the library drops any other
    key). A number carries no personal data, unlike the hero's name.
    """
    return {"powers_count": len(snapshot.get("powers") or [])}


def get_hero_event_source() -> EventSource:
    """Return the shared EventSource for Hero's `GET <prefix>/events` route."""
    return _hero_event_source()


HeroEventSource = Annotated[EventSource, Depends(get_hero_event_source)]


def get_hero_revision_repository(session: DBSession) -> Repository[Revision]:
    """Return a request-scoped Repository[Revision], MODE=mock-aware like Hero's own."""
    return _revision_repository(session)


HeroRevisionRepository = Annotated[Repository[Revision], Depends(get_hero_revision_repository)]


def get_hero_owner_scope(
    claims: Annotated[dict[str, Any], Depends(get_current_claims)],
) -> OwnerScope:
    """Build Hero's owner scope from the verified token claims, with no database session.

    `OwnerScope("owner_id", sub, read_scoped=False)`: every authenticated caller reads every
    hero (list/get), but `update`/`delete` (single or bulk) only ever reach heroes the caller
    created -- see craftainer_crud.interfaces.base.OwnerScope's docstring and
    docs/adrs/0011-owner-scoped-crud-example-resource.md for why Hero uses
    `read_scoped=False` rather than the fully-scoped default.

    A token without a usable `sub` is refused with 401 (RFC 6750 `invalid_token`) before an
    `OwnerScope` exists: `sub` is the only owner identity, so a signed token that lacks it
    (the validator requires `exp`, `iss` and `aud`, not `sub`) must not reach a write as an
    unowned caller, nor crash the request with a KeyError (500). Fail closed.

    Also the router's `scope_dependency`, so `GET <prefix>/events` resolves just this (token
    claims) and holds no database session for the stream's life (craftainer_crud ADR 0009).
    """
    return OwnerScope("owner_id", subject_of(claims), read_scoped=False)


HeroOwnerScope = Annotated[OwnerScope, Depends(get_hero_owner_scope)]


def get_hero_crud(session: DBSession, owner: HeroOwnerScope) -> CRUDInterface[HeroV2, HeroModel]:
    """Build a request-scoped, owner-scoped CRUD interface for Hero.

    `owner` is `get_hero_owner_scope`'s scope (from the token's `sub`, 401 without one):
    every authenticated caller reads every hero, `update`/`delete` only reach the caller's own.

    `revisions=RepositoryRevisionSink(...)`/`resource="hero"`/`actor=claims["sub"]`:
    every create/update/update_many/delete/delete_many is logged to the shared
    Revision table -- see craftainer_crud.interfaces.base.RevisionSink's own docstring and
    `GET <prefix>/revisions?id=`, added below via `revision_repository_dependency`.
    `actor` is the owner scope's value (the token's `sub`), the same pattern
    craftainer_crud.interfaces.README.md's "Do" section describes.

    `events=_hero_event_sink()`: every create/update/update_many/delete/
    delete_many **and** restore/restore_many is published for real-time streaming
    -- see craftainer_crud.interfaces.base.EventSink's own docstring and `GET <prefix>/events`,
    added below via `event_source_dependency`. The sink is wrapped in `AnalyticsEventSink`,
    which also records `hero.created`/`hero.deleted` product-analytics events (ADR 0027).

    MODE=mock uses the shared in-memory repository instead of `session` -- an
    unused AsyncSession's commit() never opens a connection, so `session` stays a
    harmless, uniform dependency across every mode rather than needing two
    differently-signatured variants of this function.
    """
    return CRUDInterface(
        schema=HeroV2,
        repository=_hero_repository(session),
        owner=owner,
        revisions=RepositoryRevisionSink(_revision_repository(session)),
        events=AnalyticsEventSink(
            _hero_event_sink(),
            url=f"/crud/v{ROUTER_VERSION}/heroes/v2",
            props_for=_hero_analytics_props,
        ),
        resource="hero",
        actor=str(owner.value),
    )


HeroCRUD = Annotated[CRUDInterface[HeroV2, HeroModel], Depends(get_hero_crud)]

READ_ROLE_NAMES = ("viewer", "editor", "maintainer", "detective")
WRITE_ROLE_NAMES = ("editor", "maintainer")
DELETE_ROLE_NAMES = ("maintainer",)
ReadRoles = Depends(require_roles(*READ_ROLE_NAMES))
WriteRoles = Depends(require_roles(*WRITE_ROLE_NAMES))
DeleteRoles = Depends(require_roles(*DELETE_ROLE_NAMES))

router = build_resource_router(
    bulk_rate_limit=bulk_rate_limit,
    prefix="",
    api_prefix=f"/crud/v{ROUTER_VERSION}/heroes/v2",
    tags=["heroes"],
    resource_label="Hero",
    resource="hero",
    item_tag="hero",
    list_tag="heroes",
    fields=("name", "powers", "power_level"),
    schema=HeroV2,
    create_schema=HeroV2Create,
    update_schema=HeroV2Update,
    crud_dependency=HeroCRUD,
    read_roles=ReadRoles,
    write_roles=WriteRoles,
    delete_roles=DeleteRoles,
    draft_schema=HeroV2Update,
    archivable=True,
    revision_repository_dependency=HeroRevisionRepository,
    event_source_dependency=HeroEventSource,
    scope_dependency=HeroOwnerScope,
    stats_enabled=True,
    stats_cache=stats_cache,
    manifest=ManifestOptions(
        registry=manifest_registry,
        name="heroes",
        api_version="v2",
        title="Heroes",
        model=HeroModel,
        owner_scoped=False,
        read_roles=READ_ROLE_NAMES,
        write_roles=WRITE_ROLE_NAMES,
        delete_roles=DELETE_ROLE_NAMES,
    ),
)
