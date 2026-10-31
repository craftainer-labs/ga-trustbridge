# app/controllers/

FastAPI routers with no resource of their own. A resource's own router
(e.g. Hero) lives in `../crud_1/` instead — see its `README.md`. The
generic CRUD router factories every resource builds on live in
`craftainer_crud/controllers/` instead — see its own `README.md`. Sits below
`crud_1` and `main` in `src/app/`'s import order — may import from any
other `app/` subpackage, but only `crud_1`/`main` may import from here
(see `../README.md`'s "Layering" section).

- `health.py` — `/health/live` and `/health/ready`.
- `audit.py` / `mock.py` — see their own module docstrings.

## RBAC

Add a role requirement to a route with `dependencies=[Depends
(require_roles("editor", "maintainer"))]` from `app.oidc` (see
`../README.md`'s "RBAC" section for the mechanism), or a module-level
`Depends(...)` constant reused across a router's routes — see
`app.crud_1.heroes.heroes_v2`'s `ReadRoles`/`WriteRoles`/`DeleteRoles`.

## Do

- Add auth to a route with `Depends(get_current_claims)` from
  `app.oidc` — a route with no such dependency is public.

## Don't

- Add a resource's own router module here — resources live in
  `../crud_1/` instead.
- Add a generic, resource-agnostic router-building helper here — that
  belongs in `craftainer_crud/controllers/` instead.
