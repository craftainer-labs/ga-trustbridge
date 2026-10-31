# FR-0019. Return 404 for operations on a nonexistent record

## Status

Implemented

## Description

The system shall respond 404 Not Found, with a message identifying
the resource type, whenever a get, update, or delete targets an id
that doesn't exist — consistently across JSON and XML routes.

## Source

API consumers. Implemented in `src/app/controllers/crud_router.py`.

## Acceptance criteria

- `GET`, `PATCH`, and `DELETE` on a nonexistent id all return 404
  with a `"<Resource> not found"`-style message, for both JSON and
  XML routes.
