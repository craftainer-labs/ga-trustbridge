# NFR-0019. Never edit or delete an applied migration

## Attribute

Data integrity / process.

## Description

A migration in `alembic/versions/` shall never be hand-edited or
deleted once it has been applied anywhere; any further change is
expressed as a new migration.

## Source

Developers; DBAs. Documented in `alembic/README.md`.

## Verification

Code review; no automated enforcement beyond convention today.
