# 0028. Architecture of the trustbridge GitHub App, and where instance code lives

## Status

Proposed. Builds on [0013](0013-valkey-over-redis-licensing.md) (Valkey
server, redis-py client), [0026](0026-resource-server-metadata-and-bff.md)
(resource-server validation, which the admin UI's BFF relies on) and
[0027](0027-product-analytics-with-track.md); all three are inherited from
template-fastapi. Companion ADRs: [0029](0029-adapter-plugin-interface.md)
(adapter plugin interface; it uses the tables and lease defined here) and
[0030](0030-key-signer-and-github-hosts.md) (key signer and GitHub hosts;
its startup checks run in the lifespan defined here).
Tenancy/RLS (0031) and the audit log (0032) are separate, later ADRs.

## Context

trustbridge is a self-hosted GitHub App that provisions projects,
repositories and CI credentials on artifact and deployment platforms
(JFrog, Harbor/MSR, Docker Hub, Argo CD, GHCR) and writes the matching
secrets, variables and environments back to GitHub. It must run on
github.com, GitHub Enterprise Server (GHES) and GHE.com, and meet the
enterprise baseline (EB-1 to EB-15 in the family plan).

The repo is an instance of `template-fastapi` (v0.2.0). That template's
`src/` and `tests/` are `replace` tier. So the first question is whether
instance code can live under `src/` at all. Findings from this repo:

1. **Files that exist only in the instance survive a sync.**
   `.github/template-sync-manifest.yml:157-158` lists `src/` and `tests/`
   under `replace`. `apply_replace`
   (`.github/scripts/template_sync.py:230-260`) works from the template's
   old and new checkouts only: `old_paths`/`new_paths` are filtered by
   `(old_root / p).exists()` and `(new_root / p).exists()` (lines
   234-235), built by `tier_paths(args.old_ref/new_ref, ...)` (lines
   382-385). It copies new-ref files over the instance (237-250) and
   deletes only `old_paths - new_paths` (253), i.e. files the template
   itself removed. An instance-only path such as `src/trustbridge/x.py`
   is in neither set and is never touched.
2. **A local ignore entry does not take precedence, and breaks the
   check.** The local manifest accepts only `ignore`
   (`.github/scripts/template_sync_manifest.py:66-82`) and is read only by
   the completeness check, never by sync. `check()` (lines 85-97) fails
   any tracked path matching more than one tier. A local
   `ignore: src/trustbridge/` would therefore match both `replace` and
   `ignore` and fail. Instance-only files under `src/` need no entry:
   they match `replace` once, which passes.
3. **Template-owned wiring files are overwritten.** `src/app/main.py`
   (fixed router wiring, lines 212-241; no plugin hook),
   `src/app/config.py`, `alembic/env.py`, `Dockerfile`
   (`template-sync-manifest.yml:110`), `scripts/runner.sh` (line 123) and
   `compose.yml` (line 118) are all `replace`. Editing any of them in the
   instance is undone by the next sync. `pyproject.toml` (merge),
   `alembic/versions/` (ignore, manifest line 177) and `docs/adrs/`
   numbered files (ignore, line 170) are instance-safe.
4. The packaging and import-lint config lives in `pyproject.toml`
   (`packages = ["src/app"]`, importlinter `root_packages = ["app"]`),
   which is merge tier, so the instance can extend both.
5. **What the template does about migrations at startup.**
   `src/app/main.py:49-58` defines `_run_migrations`
   (`command.upgrade(Config("alembic.ini"), "head")`), and the template's
   `lifespan` (`main.py:86-104`) calls it in the api process when
   `settings.run_migrations_on_startup` (default `true`,
   `src/app/config.py:110`) and `mode != "mock"`. `scripts/runner.sh`
   only starts uvicorn or granian (lines 9-30) and never migrates;
   `scripts/post-create.d/20-migrate.sh` runs `alembic upgrade head` in
   the devcontainer only. A worker or reconciler does not import
   `app.main`, so it never migrates. The template's own guidance is to set
   `RUN_MIGRATIONS_ON_STARTUP=false` whenever several replicas could race
   or a canary runs, and to run `alembic upgrade head` as a Job
   (`docs/architecture.md:156` and `:275-282`, NFR-0027).
6. **Settings sources.** `Settings` is `BaseSettings` with
   `SettingsConfigDict(extra="forbid")` and no `env_file`
   (`src/app/config.py:41`). pydantic-settings applies `extra` to the
   environment source only for names matching a field, so unrelated
   (and misspelt) environment variables are silently ignored; it does
   apply `extra` to dotenv data, so a dotenv file containing entries that
   match no field raises `ValidationError` under `forbid`. Sources: see
   Sources.

## Decision

### Layout: an instance package beside `app`

- Instance code lives in **`src/trustbridge/`**, a sibling package of the
  template's `src/app/`. Sub-packages: `webhooks/`, `worker/`,
  `reconciler/`, `github/`, `adapters/<name>/`, `tenancy/`, `audit/`,
  `rbac/`, `models/`, `queue/`, `startup.py`, `settings.py`, `main.py`.
  This is the standing design. A template hook (for example an
  entry-point for routers or lifespan, or an `APP_MODULE` variable in
  `runner.sh`) would be a possible future simplification, not a
  prerequisite.
- `pyproject.toml` (merge tier) gets `packages = ["src/app",
  "src/trustbridge"]` and importlinter `root_packages = ["app",
  "trustbridge"]` plus a contract that `app` never imports `trustbridge`.
  `trustbridge` may import `app`.
- **Entry point `trustbridge.main:app`** imports `app.main:app` (the
  template app: health, auth, problem details, security headers,
  telemetry, lifespan), mounts trustbridge's routers on it and wraps its
  lifespan (next subsection). Nothing in `src/app/` is edited.

### Lifespan: a trustbridge lifespan wrapping the template's

The template builds one module-level app: `app = FastAPI(...,
lifespan=lifespan)` (`src/app/main.py:197-199`); its lifespan migrates
(when enabled), prefetches JWKS, starts cache invalidation and yields
`None` (lines 86-113), so FastAPI's own hook is taken and a second
`lifespan=` cannot be passed after the fact. FastAPI passes that
`lifespan` to its router (`fastapi/applications.py:990`), and Starlette's
`Router.lifespan` enters `self.lifespan_context` when the server starts
the lifespan (`starlette/routing.py:639-648`, installed version), so
replacing the attribute with a wrapper composes both without editing a
template file. `trustbridge/main.py`:

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.health_checks import get_health_registry
from app.main import app                      # template app: routers, middleware, its lifespan
from trustbridge.settings import get_trustbridge_settings
from trustbridge.startup import SignerHealthCheck, run_startup_checks
from trustbridge.webhooks import router as webhooks_router

_template_lifespan = app.router.lifespan_context   # the template's, as FastAPI built it


@asynccontextmanager
async def _lifespan(app_: FastAPI) -> AsyncIterator[object]:
    settings = get_trustbridge_settings()          # validates; refuses bad config
    runtime = await run_startup_checks(settings)   # ADR 0030: settings, signers, issuers (no schema gate yet)
    get_health_registry().register(SignerHealthCheck(runtime))   # readiness hook (same cached registry the health router uses)
    try:
        async with _template_lifespan(app_) as state:   # template's migrate (dev/compose/tests only), JWKS, cache, GC freeze
            await runtime.wait_for_schema()        # schema gate, after any template migration
            yield state
    finally:
        await runtime.aclose()                     # trustbridge's own clients, after the template's shutdown


app.router.lifespan_context = _lifespan
app.include_router(webhooks_router, prefix="/webhooks")
```

Order: trustbridge's startup checks (settings, signers, issuers) run
first, so a bad signer or issuer refuses to start before the template
touches the database. The schema gate runs after the template's lifespan
has been entered, because in dev, compose and tests the template's own
lifespan performs the migration (`RUN_MIGRATIONS_ON_STARTUP=true`) and
the gate would otherwise fail on an empty database; in production the
template migrates nothing and the gate simply waits for the migrate Job.
On shutdown the template unwinds first, then trustbridge's resources.
`lifespan_context` is an attribute of Starlette's router, not a documented
extension point; the instance smoke test enters the lifespan with
`TestClient(app)` as a context manager and asserts the startup checks ran
and the webhook route exists, so a sync that changes the template's
construction fails visibly. The worker, reconciler and migrate commands
do not use FastAPI, so they call the same `run_startup_checks` from their
own `main()` and then call `wait_for_schema()` (see "Processes and
database roles").

### Settings

`trustbridge.settings.TrustbridgeSettings` subclasses `app.config.Settings`
and adds the GitHub hosts, signers, queue, retention and adapter fields;
it sets `extra="forbid"` explicitly and configures **no `env_file`**. Both
classes read the same environment: the template keeps `get_settings()`
(base fields), trustbridge code uses `get_trustbridge_settings()`.

Correction to the earlier wording: the two classes coexist because the
environment source ignores unrelated variables (the template tests this in
`tests/unit/test_config.py`), not because `forbid` is harmless. `forbid`
catches unknown init kwargs and dotenv entries only. So (a) trustbridge
never adds a dotenv or secrets-dir source to `TrustbridgeSettings`
without `dotenv_filtering="only_existing"`, because a shared dotenv file
would otherwise fail on every key one of the two classes does not know;
(b) a misspelt trustbridge variable is silently ignored, so settings
whose absence is dangerous (`GITHUB_HOSTS`, `SIGNERS`) have no default and
fail validation when missing. Trustbridge-only variables are not prefixed,
so a typo cannot be told from an unrelated variable by prefix; instead
startup compares every environment variable name with the trustbridge
field names (`difflib.get_close_matches`, cutoff 0.85) and fails on a
near-miss that is not itself a field name (`LEASE_TTL_SECOND`), and logs
the effective value of every trustbridge setting that has a default
(retention, TTLs, caps) so a silently applied default is visible.
Production also refuses
`RUN_MIGRATIONS_ON_STARTUP=true` (below). Installed pydantic-settings is
2.15.0.

### Processes and database roles

`scripts/runner.sh:15` and `:27` hardcode `app.main:app` and the
`Dockerfile` (replace tier, `ENTRYPOINT ["runner.sh"]`, line 112) cannot
be edited. The image therefore contains both packages (`COPY src ./src`,
`Dockerfile:50`) but its default entrypoint serves the template app.
Deployments override the entrypoint. The Helm chart (NG7) and the
instance's compose override set `command`/`entrypoint` explicitly. A plain
`docker run` of the image serves the template app (no webhooks); this is
documented in the README. No local-manifest ignore entries are needed.

| Process | Command | DB role | Migrates? | Scales |
| --- | --- | --- | --- | --- |
| api | `uvicorn trustbridge.main:app ...` | `trustbridge_app` | never; `RUN_MIGRATIONS_ON_STARTUP=false` | replicas |
| worker | `python -m trustbridge.worker` | `trustbridge_app` | never | replicas (consumer group) |
| reconciler | `python -m trustbridge.reconciler` | `trustbridge_app` | never | 1 active for singleton duties (advisory lock, EB-6); the relay runs on every replica (`SKIP LOCKED`) |
| migrate | `python -m trustbridge.migrations upgrade` | `trustbridge_owner` | yes, only this | one Job per release, before the rollout |

- **Migrate job.** A Helm `pre-install,pre-upgrade` hook (NG7), the same
  image, connecting to the primary directly, not through a pooler
  (`docs/architecture.md:275-282`). `upgrade` takes a session-level
  Postgres advisory lock on a dedicated connection, so a second concurrent
  run waits, then runs `command.upgrade(Config("alembic.ini"), "head")`.
  It exits non-zero on failure and the rollout does not start.
- **Role split.** The owner role (`trustbridge_owner`, given to the Job
  through the `POSTGRES_USER`/`POSTGRES_PASSWORD` variables the template
  already reads) owns the tables, creates `trustbridge_app` and the RLS
  policies and grants `SELECT, INSERT, UPDATE, DELETE` on tenant tables
  (hand-written revisions, ADR 0031). The runtime processes get
  `trustbridge_app` through the same variables, a non-owner subject to RLS
  with no DDL rights. The template's `RUN_MIGRATIONS_ON_STARTUP=true` would
  fail for that role, so `TrustbridgeSettings` **refuses to start with
  `mode=production` and `RUN_MIGRATIONS_ON_STARTUP=true`**. Dev, compose and
  tests keep the template default and migrate through the template's
  startup and `post-create.d/20-migrate.sh` as before, as the owner.
- **Schema gate.** Every runtime process checks at startup that
  `alembic_version` is at or after `REQUIRED_REVISION`, a constant in
  `trustbridge.startup` updated in the commit that first needs a schema
  change (so a rollback to older code against a newer schema still starts,
  per NFR-0027's expand/contract rule). `wait_for_schema()` retries for
  `SCHEMA_WAIT_SECONDS` (default 300) in every process, the api included
  (a lifespan that has not yet yielded is not serving or ready), and then
  fails (non-zero exit). A pod that starts before the Job finishes
  therefore waits instead of racing it.

### Migrations: one history, extended through a thin wrapper

`alembic/env.py` (replace) builds `target_metadata` from
`craftainer_crud.models.base.Base` and imports only `app.models.hero`
(lines 12-19, 29). `alembic/versions/` is `ignore` tier, so instance
revisions are kept.

We will keep **one linear history in `alembic/versions/`** and put
trustbridge's models on the same `Base`. Revisions are hand-reviewed
anyway; upgrading needs no metadata, so the template's `env.py` applies
them unchanged (from the migrate job, or from the template's startup in
dev). For autogenerate we add `python -m trustbridge.migrations revision
--autogenerate`, a wrapper that imports `trustbridge.models` (registering
the tables on `Base`) before calling Alembic's own CLI entry point, so the
unmodified `env.py` sees the full metadata. Plain `alembic revision
--autogenerate` would see trustbridge tables as extra and propose dropping
them; two guards: a unit test in `tests/` (instance-added files under
replace-tier `tests/` survive, finding 1) that runs
`alembic.autogenerate.compare_metadata` against a migrated database and
fails on drift, and Squawk's `ban-drop-table` in the existing lint.

Why not a second Alembic environment (own `versions` directory and
version table)? `.github/workflows/migration-compat.yml` triggers on
`alembic/versions/**` and `src/app/models/**` and
`scripts/lint-migrations.sh` takes `alembic/versions/` paths, both
template-owned (replace). A second history would escape the N-1
compatibility job and the Squawk lint. With the single history both keep
working. NG3 verifies this in the integration lane (migrate a fresh
database, run the template suites) before NG3 merges.

### Persistence and serialisation

Tables (all tenant-owned ones under RLS, ADR 0031; names are the
contract between 0028, 0029 and the later ADRs):

| Table | Purpose |
| --- | --- |
| `tenants` | surrogate `id` (UUID) for `(host, installation_id)`, unique on that pair |
| `webhook_deliveries` | the ingest outbox and dedupe record; see "Request path" |
| `scope_leases` | `(tenant_id, adapter, lease_key)` primary key; `holder`, `fence` (bigint), `expires_at` |
| `adapter_resources` | `(tenant_id, adapter, resource_key)` primary key; `ResourceState` as `name`, `ref` (jsonb) |
| `adapter_credentials` | `(tenant_id, adapter, scope_key)` primary key; `CredentialState` as jsonb; `version` |
| `credential_operations` | write-ahead log: `id`, `tenant_id`, `adapter`, `scope_key`, `kind` (`issue`, `rotate`, `delete`), `generation`, `name`, `state` (`intent`, `platform_done`, `delivered`, `awaiting_retire`, `finished`, `failed`), `attempts`, `fence`, `created_at`, `updated_at`; a partial unique index allows one non-terminal row per `(tenant_id, adapter, scope_key)` |

**Lease.** Serialisation is per `(tenant, adapter, lease_key)`.
`lease_key` is the scope's `scope_key`, except for an adapter that
declares `credential_limit`, where it is the constant `*` so a platform
cap is counted without a concurrent creator. `lease_key` is only the
lease's key: `adapter_credentials` and `credential_operations` always
carry the real `scope_key`, so scopes of one tenant keep separate state
and separate one-non-terminal-operation slots (a scope parked at
`NEEDS_MANUAL_ACTION` does not block the others). It is a lease
row, not a Postgres advisory lock: the lock must outlive a transaction
while an HTTP call runs, and session-level advisory locks do not survive
PgBouncer in transaction mode (`DB_POOL_MODE=external`, ADR 0022), while
holding a transaction open across a platform call would pin a connection
and an RLS setting. Acquire:

```sql
INSERT INTO scope_leases (tenant_id, adapter, lease_key, holder, fence, expires_at)
VALUES (:t, :a, :k, :me, 1, now() + :ttl)
ON CONFLICT (tenant_id, adapter, lease_key) DO UPDATE
   SET holder = :me, fence = scope_leases.fence + 1, expires_at = now() + :ttl
 WHERE scope_leases.expires_at < now()
RETURNING fence;
```

`:me` is a fresh UUID generated per unit of work (one task handling one
scope), never a worker or consumer id, so a redelivery of the same message
to the same consumer, or two tasks in one process, cannot both hold the
lease. No row returned means someone else holds it: the message goes back
to the retry path (below) without counting as a failed attempt. The holder
renews at a third of the TTL (`LEASE_TTL_SECONDS`, default 120) with
`UPDATE scope_leases SET expires_at = now() + :ttl WHERE ... AND holder =
:me AND fence = :fence` (no fence bump, so the `fence` the holder keeps in
memory stays valid; zero rows updated means the lease was lost and the
unit of work aborts). When the unit of work ends the holder releases with
`UPDATE ... SET expires_at = now() WHERE ... AND holder = :me AND fence =
:fence`, so the next message for the scope does not wait out the TTL. Every
write to `credential_operations`, `adapter_credentials` and
`adapter_resources` carries `AND EXISTS (SELECT 1 FROM scope_leases WHERE
... AND fence = :fence AND holder = :me)`, so a holder that lost the lease
cannot commit. The platform call itself cannot be fenced, so each adapter
call runs under a timeout of at most half the TTL
(`ADAPTER_CALL_TIMEOUT_SECONDS`, default 30); a stale holder is therefore
out of its call before the next one starts. Residual: a process frozen
longer than the TTL inside a call can still complete it; the deterministic
name and the lookup rule (ADR 0029) make that call idempotent.

**Write-ahead.** For every secret-bearing adapter call the core commits
the intent (`credential_operations` row and intent state with the
deterministic name) before the call, persists the returned state after it,
parks returned secret values in the operation value store, and only then
writes GitHub. The exact sequence and its crash windows are in ADR 0029
("Semantics"). The reconciler resumes every non-terminal operation whose
lease is free: `intent` and `platform_done` are retried, `awaiting_retire`
past `retire_after` runs `rotate_finish`, `failed` triggers cleanup, and
a sweep deletes credentials that no slot or operation accounts for.

### Request path and processing

1. **Ingress** `POST /webhooks/github/{host}` (one route per configured
   GitHub host, so the verifier knows which webhook secret to use before
   the payload is parsed; GitHub documents: HTTPS, secret,
   signature validation, a 2xx within 10 s, process asynchronously,
   `X-GitHub-Delivery` for replay detection, redelivery after downtime).
   This is a **transactional outbox**:
   - Reject at once with 413 when `Content-Length` exceeds
     `WEBHOOK_MAX_BODY_BYTES` (default 25 MiB, GitHub's documented payload
     cap; NG3 confirms), read the raw body as a stream that aborts when the
     cap is passed, and verify
     `X-Hub-Signature-256` with `hmac.compare_digest` before parsing JSON.
     The secret is per App (per GitHub host, selected by the `{host}` path
     segment), never in the URL.
   - In one transaction insert into `webhook_deliveries`: `delivery_id`
     (the `X-GitHub-Delivery` value, primary key), `host`, `event`,
     allow-listed `headers` (jsonb: `X-GitHub-Event`,
     `X-GitHub-Hook-ID`, `X-GitHub-Hook-Installation-Target-ID`,
     `X-GitHub-Hook-Installation-Target-Type`; never the signature),
     `body` (bytea), `received_at`, `state='received'`,
     `relayed_at` null, `retry_at` null, `attempts` 0, `tenant_id` null
     (resolved by the worker). `INSERT ... ON CONFLICT (delivery_id) DO NOTHING`; a
     conflict is a redelivery or replay and returns 2xx. Commit, then
     return 202. The 2xx is therefore sent only after the event is
     durable in Postgres, and the response never waits for Valkey.
   - After the response (a background task) the handler makes one
     best-effort relay of its own row (below) for latency, with a Valkey
     socket timeout of 1 second; a failure is ignored. No GitHub or adapter
     call happens on the request path.
2. **Relay** (a task in the reconciler, plus the best-effort call above):
   `SELECT ... WHERE state = 'received' AND relayed_at IS NULL AND
   (retry_at IS NULL OR retry_at <= now()) ORDER BY received_at FOR UPDATE
   SKIP LOCKED LIMIT n`, `XADD tb:events` with the **delivery id only**,
   `UPDATE ... SET relayed_at = now()`, commit. A crash after the `XADD`
   and before the commit relays a second time; handlers are idempotent and
   the worker skips a delivery already `processed`, and a duplicate claim
   does not touch `attempts` (below). The same task re-relays rows still
   `received`, with `retry_at` null, more than `RELAY_RECHECK_MINUTES`
   (default 15) after `relayed_at`, which recovers a Valkey that lost its
   data or a trimmed entry; rows waiting in a delayed retry are left to
   their `retry_at`. The
   handler and the relay are interchangeable, so several reconciler
   standbys may run it.
3. **Worker**: the same image, command `python -m trustbridge.worker`.
   Consumer group `tb-workers`; `XREADGROUP` returns a delivery id; the
   worker loads the row (body included) from Postgres, resolves the
   `(host, installation_id)` to `tenant_id`, sets the RLS setting, runs the
   handlers, marks the row `processed` and `XACK`s. `XAUTOCLAIM` reclaims
   entries idle past a threshold from dead consumers. `attempts` on the
   row counts **failed handling only**: it is incremented when a handler
   raises (a retryable or non-retryable `AdapterError`, or any other
   error), not on a claim, a redelivered duplicate, a re-relay or a busy
   lease. Retry path: for a busy lease or a retryable error the worker, in
   one transaction, sets `relayed_at = null` and `retry_at = now() +
   backoff` (bounded exponential, 5 s to 5 min; for a failure also
   `attempts + 1`) and then `XACK`s the entry; the relay re-adds the row
   once `retry_at` passes. After `MAX_DELIVERY_ATTEMPTS` failures the
   worker sets the row `dead`, `XADD`s `{delivery_id, error}` to
   `tb:events:dead`, and `XACK`s. The dead-letter stream carries no body:
   the admin API reads the body from the row, and "requeue" resets the row
   to `received` with `relayed_at` null, `retry_at` null and `attempts` 0. A row whose body was purged (below) before it was
   processed goes to `dead` with reason `body_purged`. Handlers are
   idempotent (reconcile-style, ADR 0029), so at-least-once delivery is
   safe. Leader election is not needed for workers (the consumer group
   partitions work, the lease serialises scopes); the reconciler uses a
   Postgres advisory lock (EB-6) on a direct connection for its singleton
   duties.
4. **Delivery reconciler**: GitHub never redelivers on its own. A
   periodic job lists failed deliveries with the App-JWT endpoints
   (`GET /app/hook/deliveries`, `POST /app/hook/deliveries/{id}/attempts`)
   from the last cursor and redelivers those whose ID is **missing from
   `webhook_deliveries`** (the ingress was down or answered 5xx before
   committing). A row that exists is never redelivered: it is already
   durable and the relay and worker own it.
5. **Retention and trimming** (EB-15):
   - `body` is set null (`body_purged_at`) `DELIVERY_BODY_RETENTION_DAYS`
     (default 7) after `processed`, and `DEAD_LETTER_RETENTION_DAYS`
     (default 30) after `dead`. The row (id, host, event, state) stays for
     `DELIVERY_ID_RETENTION_DAYS` (default 30) so a late redelivery is
     still deduplicated, then is deleted.
   - `tb:events` is trimmed by the reconciler with `XTRIM tb:events MINID
     <t>`, where `t = min(oldest pending id of the group, now -
     STREAM_RETENTION_HOURS)` and, with nothing pending, `t = now -
     STREAM_RETENTION_HOURS`. The stream is therefore bounded by the
     retention window even when the group is stalled or lagging (signer or
     schema-wait outage), and entries being worked (pending) are not
     trimmed. An entry trimmed unread is recovered by the re-relay sweep
     (the row is still `received`, `relayed_at` older than the recheck
     window), because Postgres, not the stream, is the source of
     truth. `tb:events:dead` is trimmed by age,
     `DEAD_LETTER_RETENTION_DAYS`.
6. **Installation tokens**: minted with the signer-backed App JWT
   (ADR 0030), cached in process until about 5 minutes before the
   1-hour expiry, and **down-scoped** per repository and permission set
   at mint time. As ADR 0030 decides, githubkit's App auth strategies
   need the PEM and are not used: trustbridge assembles the JWT through
   the signer, calls `POST /app/installations/{id}/access_tokens` itself
   and hands githubkit only a token auth strategy.

### Tenancy

Tenant = GitHub installation (EB-3). Installation IDs are unique only
per GitHub host, so the tenant is identified by `(host, installation_id)`
and every tenant-owned table carries a surrogate `tenant_id` (UUID) that
maps to that pair. Every tenant-owned table has a `tenant_id` and Postgres
row-level security keyed on a session setting
(`SET LOCAL trustbridge.tenant_id`) applied per transaction by a
dependency. Details (policies, the owner/app role split, the audit
interplay) are ADR 0031. The worker resolves the event's
`(host, installation_id)` to the `tenant_id` and sets the setting before
any tenant query. `webhook_deliveries` is the exception: it is written
before the tenant is known, so it is host-scoped and outside tenant RLS;
the relay and reconciler iterate tenants (setting the value per tenant)
for lease, operation and state tables. ADR 0031 must confirm this
exception and the grants (open issue below).

### Queue client: redis-py against a Valkey server

Follow ADR 0013: the server is Valkey, the client is `redis` (redis-py,
`redis.asyncio`), already pinned at `redis==8.1.0` and used by the
template's `app/cache.py`. trustbridge uses `redis.asyncio` for Streams
too (`xadd`, `xreadgroup`, `xautoclaim`, `xack`, `xtrim`, `xpending`,
`xinfo_groups`). Reasons: one client and one connection configuration
(`redis_url`, with the template's `rediss://` enforcement) for both
template and instance code; the stream commands used are part of the
shared protocol, which Valkey implements. NG1 also added `valkey==6.2.0`
(valkey-py), which nothing imports; **NG3 removes it**. If Valkey ever
diverges from Redis protocol in a way that matters, switching clients is a
contained change behind `trustbridge.queue`.

### Hosts, signers and adapters

All configuration is environment-driven through `TrustbridgeSettings`
(secrets from files or an external secret store, never in the image).
GitHub hosts (`GITHUB_HOSTS`) and signers (`SIGNERS`, a map from name to
signer configuration; each host references one by name) are defined in ADR
0030; adapters are discovered through the `trustbridge.adapters`
entry-point group and enabled by name (ADR 0029). Telemetry and outbound
calls other than to configured hosts and adapters are off by default
(EB-5).

## Decisions for the user (G-api)

Proposals made in this ADR that the user should weigh. Each lean is
labeled as a lean; the defaults named in the text (retention days,
TTLs) are proposals too.

1. **Ingest as a transactional outbox** (body and headers stored in
   `webhook_deliveries` in the 2xx transaction; a relay XADDs only the
   delivery id; worker and dead-letter read from Postgres). Replaces the
   earlier `enqueued_at` column. Trade-off: payload bodies live in Postgres
   for a retention window (EB-15) and there is a relay task, in exchange
   for no lost events when Valkey is down or loses data and a well-defined
   stream trim. Alternative: `enqueued_at` with the body in the stream
   (rejected: the stream is then the only copy). Lean: outbox.
2. **Tenant identity**: a surrogate tenant UUID mapping to
   `(host, installation_id)`, because installation IDs are unique only
   per GitHub host. Lean: as written.
3. **Per-host webhook route** `/webhooks/github/{host}` so the right
   webhook secret is chosen before parsing. Alternative: try every
   configured secret on a single route. Lean: per-host route.
4. **Serialisation by a lease table with fencing** rather than Postgres
   advisory locks. Trade-off: one more table and the fencing predicate on
   writes, in exchange for working behind PgBouncer transaction mode and
   across long platform calls. Lean: lease table.
5. **Write-ahead `credential_operations` and a resumable operation state
   machine** (details in ADR 0029). Lean: as written.
6. **A dedicated migrate job and no startup migration in production**
   (`TrustbridgeSettings` refuses `RUN_MIGRATIONS_ON_STARTUP=true` in
   production; a schema gate on `REQUIRED_REVISION`; owner role for the
   Job, `trustbridge_app` for runtime). Trade-off: one more deployable
   step, in exchange for no migration races between replicas, workers and
   reconcilers and a runtime role without DDL. Lean: as written.
7. **Wrapping the template's lifespan by assigning
   `app.router.lifespan_context`** from `trustbridge.main`, guarded by a
   smoke test. Alternatives: a template hook (NX-style change, declined
   for now); running the checks only in the worker/reconciler and leaving
   the api unchecked (rejected: the api mints tokens). Lean: as written.
8. **Settings hygiene**: no dotenv source in `TrustbridgeSettings`;
   dangerous settings have no default; unknown `TB_*` variables fail
   startup. Lean: as written.
9. **Retention and tuning defaults** (body 7 days, dead 30, delivery id 30,
   stream 24 h, lease TTL 120 s, adapter call timeout 30 s, relay recheck
   15 min, schema wait 300 s). Lean: approve as starting points; they are
   settings.

## Open issues

- ADR 0031 must settle RLS for `webhook_deliveries` (host-scoped, written
  before the tenant is known) and how the relay and reconciler read across
  tenants without a `BYPASSRLS` role (proposed: iterate tenants, setting
  the value per tenant; relay reads only the host-scoped table).
- The operation value store (where parked secret values live between
  issue and delivery) is specified in NG3; ADR 0029 fixes only its
  contract.
- The 25 MiB payload cap is GitHub's documented limit as recalled for
  this ADR and was not re-fetched in this pass; NG3 confirms it.
- `lifespan_context` is not a documented Starlette extension point; the
  smoke test is the guard.
- The unauthenticated webhook route buffers up to the body cap before the
  signature can be checked. Rate limiting per source address at the
  ingress controller (or an application limiter) is not decided here; it
  is a deployment question for NG7 and the user.

## Consequences

- Instance code is safe from sync with no manifest entries, and the
  template's own tests, N-1 job and lint keep covering the schema.
- The cost: two packages, a wrapper entrypoint, and an entrypoint
  override at deploy time. A released image run without the override
  serves the template app. Documented; the Helm chart makes the override
  explicit.
- If the template changes `app.main` in a way that stops
  `from app.main import app` or `app.router.lifespan_context` working
  (renamed variable, factory), the entrypoint breaks. The instance has its
  own smoke test that enters `trustbridge.main:app`'s lifespan and checks
  the webhook route, so a sync PR fails visibly.
- Mutating the template's `app` after import is simple but couples us to
  its router order and middleware stack; accepted.
- Hand-written revisions plus a wrapper CLI is a convention people can
  forget; the drift test is the backstop.
- At-least-once processing needs every handler to be idempotent; ADR 0029
  makes that an interface rule.
- Storing bodies adds a retention duty (EB-15) and database size
  proportional to webhook volume; the purge job bounds it.
- Four process types and a migrate hook make a fuller Helm chart (NG7).

## Sources

- Webhook best practices:
  https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks
- Validating webhook deliveries:
  https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries
- Handling failed deliveries (redelivery, `GET /app/hook/deliveries`):
  https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries
- GitHub App best practices:
  https://docs.github.com/en/apps/creating-github-apps/about-creating-github-apps/best-practices-for-creating-a-github-app
- Installation access tokens (1 hour, down-scoping):
  https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app
- githubkit authentication (`AppAuthStrategy`,
  `AppInstallationAuthStrategy`, `base_url`):
  https://github.com/yanyongyu/githubkit (docs: usage/getting-started/authentication.md)
- redis-py streams (`xadd`, `xreadgroup`, `xautoclaim`, `xtrim` `MINID`):
  https://redis.readthedocs.io/en/stable/commands.html
- Valkey stream commands:
  https://valkey.io/commands/#stream
- PostgreSQL row security:
  https://www.postgresql.org/docs/current/ddl-rowsecurity.html
- PostgreSQL `INSERT ... ON CONFLICT` and `SELECT ... FOR UPDATE SKIP LOCKED`:
  https://www.postgresql.org/docs/current/sql-insert.html,
  https://www.postgresql.org/docs/current/sql-select.html
- Alembic CLI and autogenerate: https://alembic.sqlalchemy.org/en/latest/autogenerate.html
- pydantic-settings, dotenv support and `extra` (context7
  `/pydantic/pydantic-settings`, docs/index.md "Dotenv (.env) support";
  `dotenv_filtering`):
  https://docs.pydantic.dev/latest/concepts/pydantic_settings/
- FastAPI lifespan events: https://fastapi.tiangolo.com/advanced/events/
  (the `lifespan_context` attribute itself is read from the installed
  Starlette source, `routing.py:591-648`, and FastAPI `applications.py:990`)
- Template migration guidance: `docs/architecture.md:156` and `:275-282`,
  `docs/nfrs/NFR-0027-expand-contract-migrations.md`, ADR 0022
