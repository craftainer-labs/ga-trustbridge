# FR-0015. Restrict Hero operations by role

## Status

Implemented

## Description

The system shall require role `viewer`, `editor`, `maintainer`, or
`detective` to read Hero data; `editor` or `maintainer` to create or
update; and `maintainer` alone to delete.

## Source

Security/compliance; product owner. Implemented in
`src/app/controllers/heroes.py`.

## Acceptance criteria

- A token with only `viewer` can read heroes but any write or delete
  attempt is rejected with 403.
- A token with only `editor` can create/update but not delete.
- A token with only `maintainer` can read, write, and delete.
- End-to-end journeys per role exist under `tests/e2e/{viewer,editor,maintainer}/`.
