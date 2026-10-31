# app/views/

The View layer: each resource's own Pydantic schemas, returned by and
accepted from `app.controllers`'/`app.crud_1`'s routes. The generic view
bases and response shapes (`ORMView`, bulk/revision/stats results) live
in `craftainer_crud/views/` instead — see its own `README.md`.

- `hero_v2.py` — the current (v2) Hero views, built on `craftainer_crud.views.base.
  ORMView`; see `../README.md`'s "Example CRUD resource: Hero".
- `hero_v1.py` — the deprecated `/crud/v1/heroes/v1` shape and its
  converter functions to/from `hero_v2.py`'s current shape; see the
  `*_vN.py` pattern below.

## Do

- Give a resource three views following `hero_v2.py`'s shape: `*Create`
  (fields accepted on create), `*Update` (the same fields, all
  `| None = None`, for partial updates), and the plain name (the full
  read view, with `id`), each subclassing `craftainer_crud.views.base.ORMView`.
- For a deprecated API version, add a `*_vN.py` module following
  `hero_v1.py`'s shape: that version's own `*Base`/`*Create`/`*Update`/
  plain-name views, plus pure converter functions to/from the current
  version's views (no I/O — see `craftainer_crud/controllers/README.md`'s "API
  and model versioning"). A deprecated version's converters go in
  `views/`, not `craftainer_crud/`, so they stay trivially unit-testable in
  isolation from the HTTP layer.

## Don't

- Import from `app.models`, `app.controllers`, or `app.health_checks` — see
  `../README.md`'s "Layering" section. Importing from `craftainer_crud.*` is fine —
  a resource's view builds on `craftainer_crud.views.base.ORMView` and friends.
  A view converts to/from an ORM instance structurally
  (`from_attributes`), never by importing the model class.
- Add generic, resource-agnostic view code here — that belongs in
  `craftainer_crud/views/` instead.
