# FR-0002. Provide Hero CRUD under the current (v2) API

## Status

Implemented

## Description

The system shall expose `/v2/heroes` list (paginated via `skip`/
`limit`, default limit 100), create (`POST`, 201), get-by-id (`GET
/{id}`), partial update (`PATCH /{id}`), and delete (`DELETE /{id}`)
operations as the example/reference CRUD resource for this template.

## Source

API consumers. Implemented in `src/app/controllers/heroes.py` via
`src/app/controllers/crud_router.py`.

## Acceptance criteria

- `GET /v2/heroes` returns a page of heroes ordered by id ascending,
  respecting `skip`/`limit` query params.
- `POST /v2/heroes` creates a hero and returns 201 with the created
  representation.
- `GET /v2/heroes/{id}` returns the hero or 404 if it doesn't exist.
- `PATCH /v2/heroes/{id}` and `DELETE /v2/heroes/{id}` behave
  correspondingly, 404 if the id doesn't exist.
