# src/

Source layout for the installable package.

- `app/` — the FastAPI application package (`app.main:app`); see its own
  `README.md`.

The generic CRUD framework (`craftainer_crud`: router factories, interface,
repositories, model/view bases) and the observability toolkit
(`craftainer_observability`: `health/`, `logs.py`, OTel, middleware) are
no longer in this tree. They are the published `craftainer-crud` and
`craftainer-observability` packages, pinned in `pyproject.toml`, each with
its own repository, README and import-linter contracts. Paths such as
`craftainer_crud/controllers/README.md` in this repository's docs refer
to files in those packages. To edit a package and the app together, see
"Developing a package and the app together" in `../docs/TEMPLATE.md`.

## Keeping the system design doc current

A change to the general system design (a new subpackage, changed
layering/import order, a new cross-cutting flat module) makes
`../docs/system-design.md` stale — update it in the same change.

## Do

- Add new top-level modules as their own package under `src/`, each with
  its own `README.md`.
- Keep `pyproject.toml`'s `[tool.hatch.build.targets.wheel] packages`
  list in sync with what's actually here (only `src/app`).

## Don't

- Import from `tests/` — dependencies point one way.
- Put fixtures, sample data, or documentation here — those belong in
  `tests/` or `docs/`.
