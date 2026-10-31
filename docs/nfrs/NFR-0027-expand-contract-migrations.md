# NFR-0027. Expand/contract migrations: every migration works with the previous release's code

## Status

Accepted

## Attribute

Availability / deployability (canary and rolling releases), data integrity.

## Description

Every Alembic migration shall work with the code of the previous release
(N-1), so that the previous and the new version can run side by side
against one database during a canary rollout, and a rollback of the code
never needs a schema rollback.

- A breaking schema change is split across releases. *Expand*: add
  columns or tables (nullable or with a default) and backfill data in
  batches. Switch the code over. *Contract*: drop the old structures in a
  later release, after the previous release is fully rolled out.
- A single migration that converts data or structures in one step, such
  as `convert_hero_superpower_to_powers_list`, is not allowed from now
  on. Applied migrations stay untouched ([NFR-0019](NFR-0019-migrations-immutable-once-applied.md)).
- Migrations never run from a canary pod. The application is deployed with
  `RUN_MIGRATIONS_ON_STARTUP=false` and migrations run as a separate step
  before the rollout.
- New migrations are free of operations that lock or rewrite a live table
  for long (a `NOT NULL` column without a default, non-concurrent index
  creation, column type changes), unless justified in the migration.

## Source

Operations: progressive delivery (Argo Rollouts or Flagger) needs old and
new versions on one database with automatic rollback. Part of the
canary-readiness work recorded in
[`../architecture.md`](../architecture.md) ("Canary releases and
migrations"). Builds on
[NFR-0003](NFR-0003-db-model-current-shape-only.md) (parallel API versions
over one current-shape model) and
[NFR-0019](NFR-0019-migrations-immutable-once-applied.md).

## Verification

- `.github/workflows/migration-compat.yml` migrates a fresh database to
  the PR's head and runs the latest release's integration and e2e suites
  against it with `RUN_MIGRATIONS_ON_STARTUP=false`, on every PR that
  touches `alembic/versions/` or the models. It skips, and says so, until
  the latest release contains `RUN_MIGRATIONS_ON_STARTUP`.
- The `lint-migrations` prek hook (`scripts/lint-migrations.sh`) renders
  each new or changed revision to SQL and lints it with Squawk.
- Code review for what neither catches, in particular that a contract
  migration only ships after the release that stopped using the old
  structure.
