# FR-0007. Auto-manage record creation and update timestamps

## Status

Implemented

## Description

The system shall set `created_at` on record creation and `updated_at`
on every create/update, without requiring the caller to supply either.

## Source

API consumers. Implemented in `src/app/models/base.py`,
`src/app/repositories/memory.py`.

## Acceptance criteria

- A created record has both `created_at` and `updated_at` populated
  by the server, even if the caller didn't include them.
- Updating a record changes `updated_at` but not `created_at`.
