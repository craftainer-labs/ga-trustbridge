# Support databases other than Postgres

## Status

Draft. Blocked on the open questions below, and on Part A of
[`2026-10-packages-performance-observability.md`](2026-10-packages-performance-observability.md)
(the crud package extraction), because most of the work lands in
`craftainer-tool-crud`.

## Goal

Let an operator run the app on a database other than Postgres (MariaDB
and Oracle were named), selected by configuration, with every supported
database tested in CI. Postgres stays the primary, fully featured
target. Until this plan is done, Postgres is the only supported database
(Decision log of the batch plan).

## Background: where the app is Postgres-specific today

| Place | Postgres-only feature | Portable replacement |
| --- | --- | --- |
| `app/config.py` `database_url` | `postgresql+asyncpg://` only | Build the URL per dialect (`POSTGRES_*` generalized to `DB_*`), or accept a full `DATABASE_URL` |
| Driver | `asyncpg` | Driver extras: `[postgres]` asyncpg, `[mariadb]` asyncmy (SQLAlchemy's `mysql+asyncmy` dialect), `[oracle]` python-oracledb (`oracle+oracledb`, async since SQLAlchemy 2.0.25) |
| `app/models/hero.py` `powers` | `postgresql.ARRAY(String)` | A `StringList` type decorator: `ARRAY` on Postgres (`with_variant`), JSON elsewhere |
| `crud/models/revision.py` `snapshot` | `postgresql.JSONB` | `JSON().with_variant(JSONB, "postgresql")` |
| `crud/repositories/sqlalchemy.py` list filter | `column.contains(value)` on `ARRAY` | A dialect-compiled "list contains" expression via SQLAlchemy's `@compiles` extension: `@>` / `= ANY` on Postgres, `JSON_CONTAINS` on MariaDB, `JSON_EXISTS` on Oracle |
| same, stats | `func.date_trunc(bucket, created_at)` | A custom `bucket_start()` function compiled per dialect with `@compiles` (`DATE_FORMAT`/arithmetic on MariaDB, `TRUNC(ts, 'IW'/'MM'/...)` on Oracle), matching `memory.py`'s documented Monday-start weeks |
| same, stats | Aggregate `FILTER (WHERE ...)` | `func.count(case((cond, 1)))`, which is portable everywhere |
| same, bulk actions | `SET LOCAL statement_timeout = '1s'` | Per dialect: Postgres as today, MariaDB `SET STATEMENT max_statement_time=1 FOR ...`, Oracle python-oracledb `call_timeout` |
| `app/health_checks.py` | `text("SELECT 1")` (fails on Oracle without `FROM DUAL`) | `select(literal(1))`, which each dialect compiles correctly |
| `ilike` filter | Native on Postgres | SQLAlchemy already emulates it as `lower() LIKE lower()`; verify the indexes still apply |
| `alembic/versions/` | Postgres-specific DDL (e.g. the `ARRAY` conversion) | See Phase 4: applied migrations are immutable (`NFR-0019`) |
| Operations | CNPG, PgBouncer, `DB_POOL_MODE=external`, Squawk | Postgres-only by nature. Equivalents (e.g. mariadb-operator, MaxScale) are deployment notes, not app code. |

Also behaviorally different between databases, and needing tests rather
than code:
- default isolation levels (MariaDB `REPEATABLE READ`, Oracle and
  Postgres `READ COMMITTED`), which matters to the existing
  "write committed before the response" guarantee;
- timezone handling of `created_at`/`updated_at` (`server_default=now()`);
- identifier and length limits (Oracle 128-byte names, no empty strings,
  since `''` is `NULL`);
- `RETURNING` support (MariaDB ≥ 10.5 only for `INSERT`/`DELETE`).

## Approach

### Phase 1: decide scope

Answer the open questions below and record them in the Decision log:
which databases, which versions, and which support tier.

### Phase 2: portable types and queries (in `craftainer-tool-crud`)

1. Add `StringList` and portable JSON types to `crud.models`, plus the
   `@compiles` expressions (list-contains, bucket-start, statement
   timeout). Uses only SQLAlchemy's documented extension points; no
   reimplementing dialect internals.
2. Switch the repository's `FILTER` aggregates to `case()`, and the
   health check to `select(literal(1))`.
3. Add dialect-compile unit tests: compile every repository statement
   for each supported dialect and snapshot the SQL. This needs no
   database, so it runs in every PR.

### Phase 3: configuration and drivers (app)

1. `DB_DIALECT` (`postgresql` default, `mariadb`, `oracle`), with the
   URL built per dialect, or a full `DATABASE_URL` overriding it.
2. Driver extras in `pyproject.toml`. The runner image installs only the
   configured dialect's extra (build arg), so a Postgres image doesn't
   carry Oracle's client libraries.
3. Production guardrails per dialect (TLS required, as for Postgres in
   the batch plan's Phase 4).

### Phase 4: migrations

Applied migrations can't change (`NFR-0019`), and the existing chain is
Postgres-specific. Proposal:
- Postgres keeps its full history.
- A new **baseline migration** creates the current schema with the
  portable types, for fresh non-Postgres databases. It's marked as a
  separate Alembic branch base, so `alembic upgrade head` on MariaDB or
  Oracle starts from the baseline, and later migrations are shared and
  must be portable.
- Every later migration is written with portable operations, or with
  explicit per-dialect branches in the migration body.
- The batch plan's expand/contract rule (NFR-0027) and N-1 check apply
  per dialect. Squawk stays Postgres-only; other dialects get no
  linter.
- Migrating an existing Postgres deployment to another database (data
  transfer) is out of scope.

### Phase 5: testing

1. Stack fragments for each extra database under
   `.devcontainer/stack/`. This is a devcontainer change, so it needs a
   rebuild. Oracle's container image is large, so check its license
   terms and arm64 support first (open question).
2. A CI matrix workflow (`db-matrix.yml`) running integration and e2e
   tests per dialect. It runs on PRs that touch models, repositories or
   migrations, plus a nightly schedule. It isn't part of `checks.yml`,
   to keep that fast.
3. Benchmarks: run the batch plan's capacity mode once per dialect and
   record the numbers, but don't gate on them.

### Phase 6: documentation

ADR (database support tiers and the migration baseline), an NFR (every
supported dialect passes the CI matrix), and the per-dialect
configuration table in `docs/architecture.md`.

## Execution

Follows `CLAUDE.md`'s orchestrator rule and `README.md`'s "Execution
section". Provisional until Phase 1 is decided:

| Wave | Items (write sets) |
| --- | --- |
| 1 (parallel) | portable types + compile helpers (`craftainer_tool_crud/models/`, a new `compat_sql.py`); repository query changes (`repositories/sqlalchemy.py`); config + drivers (`app/config.py`, `pyproject.toml`, `Dockerfile` build arg); dialect-compile tests (`tests/unit/dialects/`) |
| 2 (parallel) | baseline migration (`alembic/versions/`, `alembic/env.py`); stack fragments (`.devcontainer/stack/<db>/`; user rebuild gate); `db-matrix.yml` |
| 3 (serial) | verification lane per dialect; docs (ADR, NFR, architecture) |

**Gates:** the user decides Phase 1, rebuilds the devcontainer after
wave 2's stack fragments, and merges every PR.

## Decision log

| Date | Decision |
| --- | --- |
| 2026-10-17 | Separate plan. Postgres remains the only supported database until this plan is done. |

## Open questions

- **Which databases and versions?** MariaDB (e.g. 11.4 LTS) and Oracle
  (e.g. 23ai) were named. Others: MySQL, SQL Server, SQLite (tests or
  small deployments)?
- **Support tier:** fully supported (CI-gated on every relevant PR) or
  best-effort (nightly only)?
- **Oracle image:** are the license terms of Oracle's Free container
  image acceptable for CI and the devcontainer, and does it run on
  arm64 runners?
- **Feature parity:** may some features be Postgres-only (e.g.
  `DB_POOL_MODE=external`, or certain stats buckets), or must every
  dialect support everything?
