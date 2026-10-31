# FR-0020. Apply pending database migrations automatically at startup

## Status

Implemented

## Description

The system shall apply any pending Alembic migrations automatically,
off the event loop, before serving traffic — except under `MODE=mock`,
where there is no database to migrate.

## Source

Operators/SRE; developers. Implemented in `src/app/main.py` (lifespan
hook).

## Acceptance criteria

- Starting the app against a database with pending migrations leaves
  the schema fully migrated before the first request is served.
- Starting under `MODE=mock` performs no migration attempt.
