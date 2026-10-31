# FR-0005. Keep the deprecated Hero v1 API fully functional

## Status

Implemented

## Description

The system shall continue to serve `/v1/heroes*` (single
`superpower: str` field) against the same underlying v2 data, via a
compatibility adapter, rather than a separate table or removing the
old version outright.

## Source

Legacy API consumers. See
[0002-api-and-model-versioning](../adrs/0002-api-and-model-versioning.md).
Implemented in `src/app/controllers/heroes_v1.py`,
`src/app/views/hero_v1.py`.

## Acceptance criteria

- Every v1 Hero CRUD operation succeeds against the same records
  created or modified through v2.
- No separate v1 database table or schema exists.
