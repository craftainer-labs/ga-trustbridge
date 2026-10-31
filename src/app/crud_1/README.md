# app/crud_1/

Resource routers for the `/crud/v{ROUTER_VERSION}` API: one subpackage per
resource, each combining that resource's versioned sibling routers (built
from `craftainer_crud/controllers/`'s generic factories) into one router,
which this package's own `__init__.py` then combines into the single
`router` `app.main` mounts at `/crud/v{ROUTER_VERSION}`. Sits after
`controllers` in `src/app/`'s import order — may import from
`app.controllers`/`craftainer_crud.controllers` (the generic router factories) or
any lower layer, but nothing else may import from here except `main`
(see `../README.md`'s "Layering" section).

- `heroes/` — the example CRUD resource; see `../README.md`'s "Example
  CRUD resource: Hero".
  - `heroes_v2.py` — the current Hero shape, mounted at `/heroes/v2/{json,xml,web}`
    by `heroes/__init__.py` below (the module itself carries no prefix).
  - `heroes_v1.py` — the deprecated sibling version, mounted at
    `/heroes/v1/{json,xml,web}`; see "API and model versioning" below.
  - `__init__.py` — combines `heroes_v2.py`/`heroes_v1.py`'s routers,
    assigning each its real `/heroes/v2`/`/heroes/v1` prefix explicitly,
    into the one `router` this package's own `__init__.py` includes.
- `analytics_events.py` — `AnalyticsEventSink`, a wrapper around a resource's `EventSink`
  that, after the wrapped sink publishes, records `<resource>.created` / `.deleted`
  product-analytics events via `track()` (ADR 0027). Hero's `get_hero_crud` uses it.
- `__init__.py` — combines every resource's router (currently just
  `heroes/`) into the one `router` `app.main` mounts at
  `/crud/v{ROUTER_VERSION}`, so the router-version segment is named once,
  at that mount site, instead of by every resource.

## Multi-version resources as a package

A resource that carries more than one API version (see "API and model
versioning" below) gets its own package here rather than a single
module, so each version's controller stays its own file (`heroes_v2.py`,
`heroes_v1.py`, ...) instead of accreting an ever-longer suffix pile in
one module. The package's `__init__.py` is the only thing this package's
own `__init__.py` ever imports for that resource — it combines every
version's own router (each built by
`craftainer_crud.controllers.crud_router.build_resource_router`, see
`craftainer_crud/controllers/README.md`'s "Generic CRUD router factories") into one
`router`, so a resource stays a single `include_router` call at the
mount site no matter how many versions it carries internally:

```python
router = APIRouter()
router.include_router(heroes_v2_router, prefix="/v2")
router.include_router(heroes_v1_router, prefix="/v1")
```

Both `include_router` calls above take an explicit `prefix` —
`heroes_v2.py`/`heroes_v1.py` each build their own router with
`build_resource_router(prefix="", ...)`, deliberately carrying none of
their own mount prefix. **A resource-version router baking its own
mount prefix into `build_resource_router`'s `prefix` argument is bad
design and must not be done** — see "Don't" below. A resource simple
enough to never need more than one version can skip the package shape
and stay a single flat module instead (still living here, not in
`controllers/`) — the package layout is what a *versioned* resource
needs, not a rule every resource follows.

## Every mount names its own segment

**Every `include_router` call in this repository passes an explicit,
non-empty `prefix` argument — no exceptions.** That applies to app-level
mounts (`app.main`), this package's resource mounts, a resource
package's per-version mounts, and the per-format mounts inside
`craftainer_crud.controllers.crud_router.build_resource_router` alike, in tests as
well as in `src/`.

The point is readability and maintainability: the full URL of any route
should be reconstructible by reading the chain of `include_router` calls
that mount it, without opening the included module to discover a prefix
it carries silently. A bare `include_router(some_router)` hides a
segment (or hides that there is none), and `prefix=""` is not a
substitute — if a mount genuinely adds no segment, restructure so the
segment it *should* own is named there instead.
The one exception is a library router whose route carries an absolute path fixed by a
standard: `api_catalog_router` serves `/.well-known/api-catalog` (RFC 9727) and so is
mounted by `app.main` with `prefix=""` (see `docs/adrs/0025-crud-manifest-and-api-catalog.md`). This is the mirror image
of the "Don't" rule below: routers are built with `prefix=""` at their
factory call, and every real segment is named at a mount site.

## API and model versioning

A resource's routes are versioned along three independent, explicit path
segments: `/crud/v{router_version}/heroes/v{model_version}/{format}` —
e.g. `/crud/v1/heroes/v2/json` (current Hero shape), `/crud/v1/heroes/v1/xml`
(deprecated shape), `/crud/v1/heroes/v2/web/form`. There is no bare
unversioned alias on any of the three axes; callers pick a router
version, a resource-shape version, and a format explicitly. See
`docs/adrs/0009-explicit-crud-router-and-model-versioning-segments.md`
for the full reasoning; it supersedes
`docs/adrs/0002-api-and-model-versioning.md`.

`router_version` (`craftainer_crud.controllers.crud_router`'s `ROUTER_VERSION`
constant) names a version of the generic router factories themselves,
not a resource's shape. It's applied exactly once, at this package's own
mount site in `app.main` (`app.include_router(crud_v1_router,
prefix=f"/crud/v{ROUTER_VERSION}")`) — a resource-version module (e.g.
`heroes_v2.py`) never bakes any part of its own mount prefix into
`build_resource_router`'s `prefix` argument, not even its own
resource-relative `/heroes/v2`; every real path segment is assigned
explicitly at the `include_router` call that mounts it (`heroes/
__init__.py` assigns `/v2`/`/v1`, this package's own `__init__.py`
assigns `/heroes`, and `app.main` assigns `/crud/v{ROUTER_VERSION}`). See "Don't" below for why a router carrying
its own baked-in prefix is bad design. The one place a resource-version
router still needs the full, absolute `/crud/v{ROUTER_VERSION}/...`
path is `build_resource_router`'s `api_prefix` argument, since that gets
baked into rendered HTML/JS at build time and can't be derived from the
`include_router` calls that do the actual mounting — see
`craftainer_crud/controllers/README.md`'s "Generic CRUD router factories". The DB
model (`app.models`) always represents
the *current* shape only — an older API version is a `views` + `crud`
concern, never a second table/model. Only `app.views` classes carry a
numeric suffix, current version included (e.g. `views/hero_v2.py`'s
`HeroV2*`, matching the already-suffixed `views/hero_v1.py`'s
`HeroV1*`).

A deprecated version follows the `*_vN.py` pattern: a `views/hero_vN.py`
module defining that version's Pydantic shape plus pure converter
functions to/from the current version's views (see
`app.views.hero_v1`), and a sibling controller module (`heroes_v1.py`)
that calls the same `build_resource_router` `craftainer_crud/controllers/
README.md`'s "Generic CRUD router factories" describes, with
`crud_dependency` pointing at a `CompatCRUD`-typed dependency instead of a
`CRUDInterface`-typed one. `craftainer_crud.interfaces.compat.CompatCRUD` is the reusable
wrapper that adapts the current version's `CRUDInterface` to speak in
the deprecated view's shape, so the deprecated router needs no new
persistence wiring — see `craftainer_crud/interfaces/README.md`. Apply
`app.http_headers.sunset(...)` via the deprecated resource-version's
`router_dependencies=[...]` `build_resource_router` argument (not per
format) so every route under it — JSON/XML/web alike — advertises
`Sunset`/`Deprecation`/`Link` headers at once; a router's own
`dependencies` merge into every route of a sub-router later
`include_router`'d into it, so one declaration on the combined router
reaches all three formats. Because that's now a single shared
declaration rather than one per format, the `Link` header points at the
successor resource-version's base, full absolute path (e.g.
`/crud/v1/heroes/v2`), not a format-specific equivalent — see
`heroes_v1.py`'s `_V2_PREFIX`.

## Do

- Add a new resource as its own subpackage here (or a flat module, if it
  will never carry more than one version): a per-request `CRUDInterface`
  builder function (`get_<resource>_crud`, depended on via
  `Annotated[..., Depends(...)]` for reuse across that router's routes,
  following `heroes/heroes_v2.py`'s shape — `craftainer_crud.interfaces.dependency.
  build_repository_provider` supplies the MODE-aware repository it
  wraps), then one `craftainer_crud.controllers.crud_router.build_resource_router(...)`
  call (or a single `build_json_router(...)` call directly, for a
  resource that only ever needs JSON).
- `include_router` a new resource's router in this package's own
  `__init__.py` with an explicit `prefix` naming the resource segment
  (e.g. `prefix="/heroes"`) — the resource's own package (e.g.
  `heroes/__init__.py`) names only the version segments below it, and no
  segment is ever baked into an individual version's
  `build_resource_router` call. See "Every mount names its own segment".

## Don't

- Put persistence or conversion logic directly in a route body — that
  belongs in `craftainer_crud.interfaces`/`craftainer_crud.repositories`; a route should stay a thin
  translation between HTTP and a `CRUDInterface` call.
- Add resource-agnostic router-building logic here — a new generic
  factory (usable by any resource) belongs in
  `craftainer_crud.controllers.crud_router`, not duplicated inside a resource
  package.
- **Have a resource-version's own router bake in any part of its own
  mount prefix** (e.g. `build_resource_router(prefix="/heroes/v2", ...)`)
  **— this is bad design and must not be done.** Call it with
  `prefix=""` instead, and assign the real prefix explicitly at the
  `include_router` call that mounts it (see `heroes/__init__.py`). A
  router that carries a hidden prefix of its own makes the mount site
  lie about what path it's actually adding — a reader has to open the
  resource module itself to learn what segment it owns, instead of
  reading it straight off the one `include_router` call responsible for
  it. This applies at every level: a resource-version router never bakes
  in its own `/heroes/vN`, and this package's own combined router never
  bakes in `/crud/v{ROUTER_VERSION}` — that segment belongs solely at
  the mount site in `app.main`.
- **Call `include_router` without an explicit `prefix`, or with an empty
  `prefix=""`.** Every mount names the segment it adds — see "Every
  mount names its own segment" above.
