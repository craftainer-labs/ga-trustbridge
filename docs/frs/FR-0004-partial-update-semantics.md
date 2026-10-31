# FR-0004. Apply only explicitly supplied fields on partial update

## Status

Implemented

## Description

The system shall apply, on `PATCH`, only the fields the caller
explicitly supplied, never overwriting an omitted field with a
schema default.

## Source

API consumers. Implemented in `src/app/crud/base.py` (`exclude_unset`).

## Acceptance criteria

- A `PATCH` that supplies only `name` leaves `powers` unchanged.
- A `PATCH` that supplies only `powers` leaves `name` unchanged.
