# 0022. Choose the database pool per deployment with `DB_POOL_MODE`

## Status

Accepted

## Context

[`0006`](0006-mode-driven-fakes-for-infrastructure-free-testing.md)
chose `NullPool` for the async engine unconditionally, so tests could
mix pytest-asyncio's event loop with `TestClient`'s background-thread
loop without a pooled asyncpg connection surviving across them.
`NFR-0016` accepted the lost connection reuse and named an external
pooler as the sanctioned fix. This ADR supersedes the "unconditionally"
part of that decision: the pool class is now deployment configuration.
The test-loop rationale stands, which is why `null` stays the default.

The cost is measurable. The 2026-10-18 baseline profile at the 1 vCPU /
2 GB reference budget ([`0019`](0019-per-pr-benchmarks-and-fixed-load-acceptance.md))
shows Python self time dominated by new TLS connections (SSL context,
handshakes, asyncpg connect, DNS): every request paid a TCP handshake,
Postgres authentication, a backend fork and statement re-preparation.

The right pool depends on where the app runs, not on the code: a single
replica talking straight to Postgres, and many autoscaled replicas
behind PgBouncer, want different engines.

## Decision

We will add `Settings.db_pool_mode` (`DB_POOL_MODE`, environment only per
`NFR-0017`), read by `app.db.build_engine(settings)`:

| `DB_POOL_MODE` | Engine | Use when |
| --- | --- | --- |
| `null` (default) | `NullPool`, as before | Tests, `dev`, `mock`; behaviour is unchanged until a deployment opts in |
| `local` | `AsyncAdaptedQueuePool` sized by `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_RECYCLE`, with `pool_pre_ping=True` | The app connects straight to Postgres and the connection budget below fits |
| `external` | `NullPool` plus `connect_args={"prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__"}` | Behind PgBouncer in transaction mode, e.g. a CloudNativePG `Pooler` |

`external` is SQLAlchemy's documented asyncpg-behind-PgBouncer recipe:
asyncpg's default numbered prepared-statement names collide across
clients that share a server connection.

**Connection budget.** replicas x workers x (`DB_POOL_SIZE` +
`DB_MAX_OVERFLOW`) must stay below Postgres's `max_connections`, minus
reserved slots and headroom for migrations and admin sessions. While it
fits, use `local`: one network hop fewer, and no extra process competing
for the single core (the reason the challenge's fastest entries dropped
Nginx). Switch to `external` when replica autoscaling pushes the total
past that limit.

The repo-root `compose.yml` and the smoke stack set `DB_POOL_MODE=local`,
so `perf.yml` measures the mode a direct deployment would use. Related
variables that make the choice usable on an HA Postgres operator
(`POSTGRES_SSLMODE`, `DB_CONNECT_TIMEOUT`, `DB_COMMAND_TIMEOUT`,
`RUN_MIGRATIONS_ON_STARTUP`) are plain settings read from the environment
and are documented in `src/app/README.md`. Lifespan shutdown disposes the
engine so pooled connections close cleanly.

**Measured effect (null vs local, reference budget, fixed load,
[`0019`](0019-per-pr-benchmarks-and-fixed-load-acceptance.md)'s
acceptance rule; baseline-relative results are in `0019`):** 19.11 CPU
ms/request and 200 ms aggregate p99 with `null`, against 7.31 CPU
ms/request (-61.7%) and 68 ms p99 with `local` (p50 11 ms, p95 62 ms).
Capacity was not measured separately for `null` and `local`; the final
`local` configuration reaches 130 users at the reference budget (10-user
steps), against 35 for the original baseline. Connection budget:
replicas x workers x (`DB_POOL_SIZE` + `DB_MAX_OVERFLOW`) must stay
below `max_connections` minus reserved slots and migration/admin
headroom.

## Consequences

Postgres stays the only supported database: the asyncpg driver, its
prepared-statement handling above, and the models' `ARRAY`/`JSONB` types
assume it. Supporting MariaDB, Oracle or another engine would be a
separate portability project, not a pool-mode setting.

Easier: a direct deployment gets connection reuse with one setting and no
extra process; a pooled deployment has a documented, unit-tested
`external` recipe; tests and `mock`/`dev` keep their loop-safe default.

Harder: `local` pools are bound to the event loop that opened them, so
anything that mixes loops (tests) must stay on `null`. The pool size is
now a number operators must budget against `max_connections`.
`external` is covered only by unit tests in this repo, because no
PgBouncer service is added to any stack; it must be live-tested on the
first deployment behind a pooler, with PgBouncer in transaction mode and
an explicit non-zero `max_prepared_statements` (the default changed
across versions), checking that prepared statements do not pile up on
server connections. The measured null-vs-local numbers are above; `external` has no
measurement because no PgBouncer was in the stack.
