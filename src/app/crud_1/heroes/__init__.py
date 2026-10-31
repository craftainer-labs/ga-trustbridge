"""HTTP routes for the Hero resource: the current (v2) and deprecated (v1) sibling versions.

`heroes_v2.py`/`heroes_v1.py` each build their own `build_resource_router(...)`
router with `prefix=""` -- neither carries any mount prefix of its own (see
`craftainer_crud.controllers.crud_router`'s "Generic CRUD router factories" and
`crud_1/README.md`'s "Don't" section for why). This module is the one that
assigns each version's own segment, explicitly, at the `include_router` call
that mounts it -- combining the two into the one `router` `app.crud_1` mounts
under `/heroes`, so a resource that gains further versions stays a single
`include_router` call at the mount site regardless of how many versions it
carries internally.

Every `include_router` call in this repository passes an explicit, non-empty
`prefix` -- see `crud_1/README.md`'s "Every mount names its own segment".
"""

from fastapi import APIRouter

from app.crud_1.heroes.heroes_v1 import router as heroes_v1_router
from app.crud_1.heroes.heroes_v2 import router as heroes_v2_router

router = APIRouter()
router.include_router(heroes_v2_router, prefix="/v2")
router.include_router(heroes_v1_router, prefix="/v1")
