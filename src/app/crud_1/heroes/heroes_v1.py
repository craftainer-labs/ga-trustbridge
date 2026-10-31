"""HTTP routes for the deprecated (v1), single-power Hero shape.

Deprecated in favor of heroes_v2.py, which supports multiple powers per
hero. Wraps the same CRUD heroes_v2.py already builds via
craftainer_crud.interfaces.compat.CompatCRUD, converting to/from the v1 view with
app.views.hero_v1's converter functions -- no new persistence code, only the
version-compatibility shape. `prefix=""` below: this router carries none of
its own mount prefix -- `app.crud_1.heroes`'s `__init__.py` assigns
`/v1` explicitly, via `include_router(router, prefix=...)`, when it
combines this with heroes_v2.py's router into the one `router` `app.crud_1`
mounts at `/crud/v{ROUTER_VERSION}` in `main.py`. See `crud_1/README.md`'s
"Don't" section for why a resource-version router should never bake in its
own prefix.
"""

from datetime import UTC, datetime
from typing import Annotated

from craftainer_crud.controllers.crud_router import ROUTER_VERSION, build_resource_router
from craftainer_crud.interfaces.compat import CompatCRUD
from craftainer_crud.manifest import ManifestOptions
from fastapi import Depends

from app.crud_1.heroes.heroes_v2 import (
    DELETE_ROLE_NAMES,
    READ_ROLE_NAMES,
    WRITE_ROLE_NAMES,
    DeleteRoles,
    HeroCRUD,
    ReadRoles,
    WriteRoles,
)
from app.crud_1.wiring import bulk_rate_limit, manifest_registry
from app.http_headers import sunset
from app.models.hero import Hero as HeroModel
from app.views.hero_v1 import (
    HeroV1,
    HeroV1Create,
    HeroV1Update,
    hero_v1_create_to_v2,
    hero_v1_update_to_v2,
    hero_v2_to_v1,
)
from app.views.hero_v2 import HeroV2

SUNSET_AT = datetime(2027, 1, 1, tzinfo=UTC)
_V2_PREFIX = f"/crud/v{ROUTER_VERSION}/heroes/v2"


def get_hero_v1_crud(crud: HeroCRUD) -> CompatCRUD[HeroV1, HeroV2, HeroModel]:
    """Build a v1-shaped CRUD interface backed by the current (v2) Hero CRUD."""
    return CompatCRUD(
        crud,
        to_legacy=hero_v2_to_v1,
        from_legacy_create=hero_v1_create_to_v2,
        from_legacy_update=hero_v1_update_to_v2,
    )


HeroV1CRUD = Annotated[CompatCRUD[HeroV1, HeroV2, HeroModel], Depends(get_hero_v1_crud)]

router = build_resource_router(
    bulk_rate_limit=bulk_rate_limit,
    prefix="",
    api_prefix=f"/crud/v{ROUTER_VERSION}/heroes/v1",
    tags=["heroes"],
    resource_label="Hero",
    resource="hero",
    item_tag="hero",
    list_tag="heroes",
    fields=("name", "superpower"),
    schema=HeroV1,
    create_schema=HeroV1Create,
    update_schema=HeroV1Update,
    crud_dependency=HeroV1CRUD,
    read_roles=ReadRoles,
    write_roles=WriteRoles,
    delete_roles=DeleteRoles,
    router_dependencies=[Depends(sunset(SUNSET_AT, link=_V2_PREFIX))],
    manifest=ManifestOptions(
        registry=manifest_registry,
        name="heroes",
        api_version="v1",
        title="Heroes (v1, deprecated)",
        model=HeroModel,
        owner_scoped=False,
        read_roles=READ_ROLE_NAMES,
        write_roles=WRITE_ROLE_NAMES,
        delete_roles=DELETE_ROLE_NAMES,
    ),
)
