"""Resource routers for the `/crud/v{ROUTER_VERSION}` API, combined into one router.

Each resource subpackage (e.g. `heroes/`) combines its own versioned sibling
routers into one router carrying only the version segments (e.g.
`heroes/__init__.py`'s router spans `/v1` and `/v2`) -- see
`craftainer_crud.controllers.crud_router`'s `build_resource_router` for how one version's
own router is built. This module names each resource's own segment, at the
`include_router` call that mounts it, and combines them into the one `router`
`app.main` mounts at `/crud/v{ROUTER_VERSION}`, so the router-version segment
is named once, at that mount site, instead of by every resource, and a new
resource stays a single `include_router` call here.

Every `include_router` call in this repository passes an explicit, non-empty
`prefix` -- see `crud_1/README.md`'s "Every mount names its own segment".
"""

from fastapi import APIRouter

from app.crud_1.heroes import router as heroes_router

router = APIRouter()
router.include_router(heroes_router, prefix="/heroes")
