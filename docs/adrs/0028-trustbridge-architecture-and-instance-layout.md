# 0028. Architecture of the trustbridge GitHub App, and where instance code lives

## Status

Proposed. The user approved the G-api decisions on 2026-11-01; the ADR becomes Accepted when the implementation (NG3-NG5) merges. Builds on [0013](0013-valkey-over-redis-licensing.md) (Valkey
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
   `lifespan` (`main.py:86-124`) calls it (lines 103-104) in the api process when
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

This ADR records decisions and their consequences. How each one must
behave under failure is stated as testable invariants (next section) that
NG3 implements and tests; the mechanics are NG3's to design within them.

### Layout: an instance package beside `app`

- Instance code lives in **`src/trustbridge/`**, a sibling package of the
  template's `src/app/`. As implemented by NG3: `main.py` (the
  entrypoint), `settings.py`, `webhooks/` (the ingress route and the
  limiter), `worker/` (the shared runtime core: outbox and stores, stream
  client, relay, worker, reconciler, leases, host availability, secret
  cache, schema gate, tenants, migrate Job) and the one-line command shims
  `reconciler/` and `migrations/`. Later items add `github/`,
  `adapters/<name>/`, `tenancy/`, `audit/` and `rbac/`. The commands are
  `python -m trustbridge.worker {worker|reconciler|migrate ...}`, and the
  shims keep `python -m trustbridge.reconciler` and
  `python -m trustbridge.migrations upgrade` working. This is the standing
  design. A template hook (for example an entry-point for routers or
  lifespan, or an `APP_MODULE` variable in `runner.sh`) would be a possible
  future simplification, not a prerequisite.
- `pyproject.toml` (merge tier) gets `packages = ["src/app",
  "src/trustbridge"]` and importlinter `root_packages = ["app",
  "trustbridge"]` plus a contract that `app` never imports `trustbridge`.
  `trustbridge` may import `app`.
- **Entry point `trustbridge.main:app`** imports `app.main:app` (the
  template app: health, auth, problem details, security headers,
  telemetry, lifespan), mounts trustbridge's routers on it and wraps its
  lifespan (next subsection). Nothing in `src/app/` is edited.

### Lifespan: a trustbridge lifespan wrapping the template's

The template builds one module-level app with `FastAPI(...,
lifespan=lifespan)` (`src/app/main.py:197-199`); its lifespan migrates
when enabled, prefetches JWKS, starts cache invalidation and yields
`None` and, in its `finally`, shuts down health checks, analytics and
the engine (lines 86-124), so the hook is taken and a second `lifespan=`
cannot be passed later. FastAPI hands that lifespan to its router
(`fastapi/applications.py:990`) and Starlette's `Router.lifespan` enters
`self.lifespan_context` at server start (`starlette/routing.py:639-648`,
installed version). **Decision:** `trustbridge.main` replaces
`app.router.lifespan_context` with a wrapper that validates trustbridge's
settings, runs the per-host startup checks (ADR 0030) and registers the
per-host availability registry, enters the template's own lifespan inside
it, and releases trustbridge's resources after the template has shut down.
No template file is edited. The worker, reconciler and migrate commands do
not use FastAPI; they call the same startup code from their own `main()`.

**Failure policy (G-api, ADR 0030 item 5).** Only invalid settings stop a
process: the wrapper raises, so the lifespan never yields. A failed
per-host check (signer, GitHub, issuer) never raises; it marks that host
`unavailable` and the wrapper still yields, so the api keeps accepting
webhooks (ingress needs no signer) and serves every other host. A host
with no App yet is `unregistered`. The wrapper starts a recheck task that
re-runs the failed checks every `HOST_RECHECK_SECONDS` and cancels it on
shutdown. The worker and reconciler commands start the same recheck loop
from their own `main()`; each process holds its own host state in memory
(there is no shared source of truth), so a worker leaves `unavailable`
host deliveries alone until its own recheck passes. The signer is not part of the api's readiness (ADR 0030,
"Startup checks and readiness").

Consequence: `lifespan_context` is an attribute of Starlette's router, not
a documented extension point, so a sync that changes the template's
construction can break this. The guard is a smoke test (invariant 20).

### Settings

`trustbridge.settings.TrustbridgeSettings` subclasses `app.config.Settings`
and adds the GitHub hosts, signers, queue, retention and adapter fields;
it sets `extra="forbid"` explicitly and configures **no `env_file`**. The
template keeps `get_settings()` (base fields); trustbridge code uses
`get_trustbridge_settings()`.

Correction to the earlier wording: the two classes coexist because the
environment source ignores unrelated variables (the template tests this in
`tests/unit/test_config.py`), not because `forbid` is harmless. `forbid`
catches unknown init kwargs and dotenv entries only, so a misspelt
environment variable is silently ignored. **Decisions:** no dotenv or
secrets-dir source on `TrustbridgeSettings` (unless filtered to existing
fields), settings whose absence is dangerous (`GITHUB_HOSTS`, `SIGNERS`)
have no default, near-miss variable names are detected at startup, and
effective defaults are logged (invariant 19). Production refuses
`RUN_MIGRATIONS_ON_STARTUP=true`. Installed pydantic-settings is 2.15.0.

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
| api | `uvicorn trustbridge.main:app ...` | `trustbridge_app` | never | replicas |
| worker | `python -m trustbridge.worker` | `trustbridge_app` | never | replicas (consumer group) |
| reconciler | `python -m trustbridge.reconciler` | `trustbridge_app` | never | replicas; singleton duties run on one at a time (EB-6) |
| migrate | `python -m trustbridge.migrations upgrade` | `trustbridge_owner` | yes, only this | one Job per release, before the rollout |

**Decisions.**

- Migrations run in a dedicated migrate Job (a Helm pre-install/pre-upgrade
  hook, NG7) against the primary directly, never in the api, worker or
  reconciler. The template's startup migration stays for dev, compose and
  tests only; production refuses it. This follows the template's own
  guidance (`docs/architecture.md:156`, `:275-282`, NFR-0027).
- Two database roles: the Job runs as the owner role that owns the tables
  and creates the application role and the RLS policies (ADR 0031);
  runtime processes run as a non-owner application role subject to RLS with
  no DDL rights. Both are supplied through the `POSTGRES_*` variables the
  template already reads.
- Runtime processes gate on the schema through one **schema-compatibility
  integer**: a single-row table (the schema-compatibility table) that every
  migration writes, compared with the integer the running code requires.
  They wait until the database's integer is at least the required one and
  stay compatible with a higher one (rollback), per NFR-0027's
  expand/contract rule (invariants 12-13).

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

Table names are the contract between this ADR, 0029 and the later ADRs
(columns are NG3's, within the invariants):

| Table | Purpose |
| --- | --- |
| `tenants` | surrogate tenant id (UUID) for `(host, installation_id)` |
| `webhook_deliveries` | the ingest outbox and dedupe record |
| `scope_leases` | per-scope leases with a fencing counter |
| `adapter_resources` | `ResourceState` of non-secret resources (ADR 0029) |
| `adapter_credentials` | `CredentialState` per credential scope (ADR 0029) |
| `credential_operations` | write-ahead log of secret-bearing operations |

**Decisions.**

- **Serialisation per scope by a lease with fencing.** One credential or
  resource operation at a time per `(tenant, adapter, lease key)`. The
  lease key is the scope's key, except for an adapter that declares
  `credential_limit`, where one adapter-wide key serialises creation so
  the platform cap is never exceeded by concurrent workers. A lease is a
  row, not a Postgres advisory lock, because the hold must outlive a
  transaction while a platform HTTP call runs and session advisory locks do
  not survive PgBouncer transaction mode (`DB_POOL_MODE=external`, ADR
  0022). Guarded writes carry the fence, so a holder that lost its lease
  cannot commit; platform calls are bounded by a timeout shorter than the
  lease TTL. Residual: a process frozen past the TTL inside a call can
  still complete it; the deterministic name and the lookup rule (ADR
  0029) make that call idempotent.
- **Write-ahead for every secret-bearing call.** The intent (operation row
  and deterministic platform-side name) is committed before the adapter is
  called; the adapter's returned state is persisted after; secret values
  are parked outside Postgres until delivered to GitHub. Rotation resumes
  the in-flight operation instead of starting another. The reconciler
  resumes every non-terminal operation whose lease is free and sweeps
  unaccounted platform credentials. The adapter-facing contract is in ADR
  0029.

### Request path and processing

1. **Ingress** `POST /webhooks/github/{host}`, one route per configured
   GitHub host so the verifier knows which webhook secret to use before
   the payload is parsed (GitHub documents: HTTPS, secret, signature
   validation, a 2xx within 10 s, process asynchronously,
   `X-GitHub-Delivery` for replay detection, redelivery after downtime).
   **Decision: a transactional outbox.** The handler first applies the
   abuse limiter (next subsection), then bounds the body size,
   verifies `X-Hub-Signature-256` in constant time, then stores the
   delivery id, host, event, allow-listed headers and the body in
   `webhook_deliveries` in one transaction and only then answers 2xx.
   Valkey is never required on the request path: the limiter is the only
   code that touches it there, and it fails open. A repeated delivery id
   returns 2xx without a second row. No GitHub or adapter call happens on
   the request path. A host with no App yet (`unregistered`, ADR 0030) has
   no webhook secret to verify with, so its route answers 503 without
   reading the body. (Such a host has no App, so GitHub sends it nothing;
   the 503 only answers a misrouted request and there is nothing to
   redeliver.)
2. **Relay.** A relay task (in the reconciler, plus one best-effort call
   from the ingress after the response) adds the **delivery id only** to
   the Valkey stream `tb:events`. It may add an id twice; processing is
   idempotent. It also re-adds rows that never reached the stream or whose
   stream entry was lost.
3. **Worker** (same image, `python -m trustbridge.worker`). Consumer group
   `tb-workers`; the worker reads the delivery id, loads the row and body
   from Postgres, resolves `(host, installation_id)` to the tenant, sets
   the RLS setting, runs the handlers and marks the row processed.
   Failures retry with backoff; a delivery that exhausts its attempts, or
   that repeatedly kills the worker, goes to the dead-letter stream
   `tb:events:dead` as `{delivery_id, error}`. The dead-letter carries no
   body: the admin API reads it from the row, and "requeue" makes the row
   deliverable again. Handlers are idempotent (reconcile-style, ADR 0029).
   A delivery whose host is not `ready` (ADR 0030) is not handled: the
   worker acknowledges the stream entry without claiming it for handling,
   so the stream's delivery count (invariant 4) is not consumed, and the
   row stays unprocessed in the outbox, uncounted as a failure. The
   re-relay sweep (item 2) re-adds it when the host is `ready` again.
   Other hosts' deliveries are unaffected.
   Leader election is not needed for workers (the consumer group
   partitions work, the lease serialises scopes).
4. **Delivery reconciler**: GitHub never redelivers on its own. A periodic
   job lists failed deliveries with the App-JWT endpoints
   (`GET /app/hook/deliveries`, `POST /app/hook/deliveries/{id}/attempts`)
   and redelivers those whose id is **missing from `webhook_deliveries`**
   (the ingress was down or answered 5xx). A stored row is never
   redelivered: it is durable and the relay and worker own it.
5. **Retention and trimming** (EB-15). Bodies are purged after a retention
   period (shorter for processed, longer for dead rows) while the row
   remains long enough to deduplicate a late redelivery. The stream holds
   ids only and is trimmed only below what the group has acknowledged;
   the dead-letter stream is trimmed by age. Postgres, not the stream, is
   the source of truth. The settings and their defaults are in the
   decision record, item 9.
6. **Installation tokens**: minted with the signer-backed App JWT
   (ADR 0030), cached in process until about 5 minutes before the
   1-hour expiry, and **down-scoped** per repository and permission set
   at mint time. As ADR 0030 decides, githubkit's App auth strategies
   need the PEM and are not used: trustbridge assembles the JWT through
   the signer, calls `POST /app/installations/{id}/access_tokens` itself
   and hands githubkit only a token auth strategy.

### Abuse limits on the webhook route

The route is unauthenticated until the signature check, and the ingress
buffers up to `WEBHOOK_MAX_BODY_BYTES` before it can verify. **Decision
(G-api item 10, option b): an application limiter in the api, not the
ingress controller.** It limits per source address, keeps its counters in
Valkey so that all api replicas share them, and **fails open**.

- **Placement.** The limiter is the first step of the webhook route
  (a dependency or a route-scoped middleware, not an app-wide one). It runs
  before the `Content-Length` check, before any body byte is read or
  buffered and before the signature check, so a request with a bad
  signature counts against its source address. A request over the limit
  gets 429 with `Retry-After` (the whole seconds left in the current
  window, at least 1) and its body is never read. GitHub treats
  that as a failed delivery, and the delivery reconciler (item 4)
  redelivers it once its host is `ready`, because listing and
  redelivering need the host's App JWT.
- **Algorithm.** A fixed-window counter: `INCR` on the key
  `tb:rl:webhooks:<address>:<window index>` and `EXPIRE` of twice the
  window on first increment, done atomically (one script or a
  transactional pipeline, NG3's choice). Over `WEBHOOK_RATE_LIMIT_REQUESTS`
  within `WEBHOOK_RATE_LIMIT_WINDOW_SECONDS` is a rejection. The key is the
  address alone, shared by all `/webhooks/github/{host}` routes. An IPv6
  address is reduced to its /64 prefix before keying.
- **Fail open.** The whole limiter check (connect, if needed, plus the one
  atomic command) is bounded by a single budget,
  `WEBHOOK_RATE_LIMIT_TIMEOUT_MS`, not per call. A connection error, a timeout or any
  other Valkey error lets the request through to normal handling and
  increments a metric and a rate-limited log line. Ingest therefore
  survives a Valkey outage through the outbox. Accepted consequence: while
  Valkey is down there is no abuse limit, and the body cap and signature
  check are the only bound.
- **Source address behind a proxy.** The limiter reads only
  `request.client.host`, the ASGI client address, and never parses
  `X-Forwarded-For` itself. `trustbridge.main` installs uvicorn's
  `ProxyHeadersMiddleware` (`uvicorn.middleware.proxy_headers`) with
  `trusted_hosts=TRUSTED_PROXIES`, so that field holds the client address.
  As uvicorn documents, the middleware only honours the headers when the
  directly connected peer is in the trusted list (IP addresses, networks or
  literals; its default without configuration is loopback only, and
  `--forwarded-allow-ips` / `FORWARDED_ALLOW_IPS` is the same list when the
  server flag is used). With a list, it combines repeated
  `X-Forwarded-For` fields in order and takes the right-most entry that is
  not itself trusted. With the literal `*` it trusts everything and takes
  the left-most entry, which the client can forge; trustbridge therefore
  refuses `TRUSTED_PROXIES=*` in production and the deployment must not set
  `--forwarded-allow-ips='*'`. `TRUSTED_PROXIES` has no default in
  production: leaving it unset is refused, and an explicitly empty list
  means "no proxy: use the socket peer". The entrypoint command disables
  uvicorn's own proxy handling (`--no-proxy-headers`), so
  `TRUSTED_PROXIES` is the only trusted list and `FORWARDED_ALLOW_IPS` has
  no effect. A deployment behind an ingress must list the ingress; an
  empty list behind one would make every client share the ingress's
  address and bucket. When
  `request.client` is `None` (a Unix socket), the key is the fixed value
  `unknown`.
- **Settings (proposed defaults).** `WEBHOOK_RATE_LIMIT_ENABLED=true`,
  `WEBHOOK_RATE_LIMIT_REQUESTS=300`, `WEBHOOK_RATE_LIMIT_WINDOW_SECONDS=60`,
  `WEBHOOK_RATE_LIMIT_TIMEOUT_MS=50`,
  `WEBHOOK_RATE_LIMIT_EXEMPT_CIDRS=[]` (addresses that bypass the limiter,
  for example GitHub's hook ranges from `GET /meta`, or a single GHES
  server address that delivers every tenant's events), `TRUSTED_PROXIES`
  (no default in production). The 300-per-minute default is deliberately
  far above one GitHub host's normal rate, so a GHES server that sends all
  its tenants' events from one address is not throttled in normal
  operation; operators with a busier host raise it or exempt it.
- **Owner.** NG3 builds and tests the limiter with the webhook route, in
  `src/trustbridge/webhooks/`. The rate limits NG6 lists are those of the
  versioned admin API (EB-11, `Retry-After`); NG6 may reuse the limiter
  component but owns only the API-side policy.

### Tenancy

Tenant = GitHub installation (EB-3). Installation IDs are unique only
per GitHub host, so the tenant is identified by `(host, installation_id)`
and every tenant-owned table carries a surrogate `tenant_id` (UUID) that
maps to that pair, with Postgres row-level security keyed on a per-
transaction session setting. Details (policies, the owner/app role split,
the audit interplay) are ADR 0031. The worker resolves the event's
`(host, installation_id)` to the `tenant_id` and sets the setting before
any tenant query. `webhook_deliveries` is the exception: it is written
before the tenant is known, so it is host-scoped and outside tenant RLS;
the relay and reconciler iterate tenants for tenant-owned tables. ADR 0031
must confirm this exception and the grants (open issue below).

### Queue client: redis-py against a Valkey server

Follow ADR 0013: the server is Valkey, the client is `redis` (redis-py,
`redis.asyncio`), already pinned at `redis==8.1.0` and used by the
template's `app/cache.py`. trustbridge uses `redis.asyncio` for Streams
too. Reasons: one client and one connection configuration (`redis_url`,
with the template's `rediss://` enforcement) for both template and
instance code; the stream commands used are part of the shared protocol,
which Valkey implements. NG1 also added `valkey==6.2.0` (valkey-py), which
nothing imports; **NG3 removes it**. If Valkey ever diverges from Redis
protocol in a way that matters, switching clients is a contained change
behind `trustbridge.queue`.

### Hosts, signers and adapters

All configuration is environment-driven through `TrustbridgeSettings`
(secrets from files or an external secret store, never in the image).
GitHub hosts (`GITHUB_HOSTS`) and signers (`SIGNERS`, a map from name to
signer configuration; each host references one by name) are defined in ADR
0030; adapters are discovered through the `trustbridge.adapters`
entry-point group and enabled by name (ADR 0029). Telemetry and outbound
calls other than to configured hosts and adapters are off by default
(EB-5).

## Invariants NG3 must implement and test

Each is one testable sentence. Where a number is a setting, the setting
name is used. Invariants about the adapter contract are in ADR 0029
("Invariants NG3 must implement and test"); those about signers and hosts
are in ADR 0030.

**Ingest and queue**

1. A 2xx for a webhook is returned only after its delivery row is
   committed in Postgres, and a repeated delivery id returns 2xx without
   creating a second row.
2. A request whose `Content-Length` exceeds `WEBHOOK_MAX_BODY_BYTES` is
   rejected with 413 before its body is read, a streamed body that passes
   the cap is aborted, and the signature is verified in constant time
   before the JSON is parsed or anything is stored. The limiter
   (invariants 25-28) runs before all of these.
3. A delivery's failure counter increases only when a handler fails;
   claiming an entry, a duplicate from the relay, a re-relay, a busy
   lease and a delivery waiting for a host that is not `ready` never
   increase it.
4. A delivery whose handling ends the worker process without recording a
   failure reaches the dead letter after `MAX_DELIVERY_ATTEMPTS`
   deliveries of the same stream entry (counted from the stream's delivery
   count), while a relay duplicate or a retry re-add does not count as a
   delivery of that entry.
5. After the stream loses data (restart without persistence, flush or
   failover), the re-relay sweep re-adds every delivery row that is
   unprocessed and not `dead`, including rows never relayed, and leaves
   rows waiting in a delayed retry until their retry time.
6. Trimming `tb:events` never removes an entry that is unread by the group
   or pending (unacknowledged), including when nothing is pending and when
   the group is stalled; group lag is exported as a metric.
7. A dead-lettered delivery's body is readable from Postgres until its
   dead-letter retention elapses, and "requeue" makes exactly that row
   deliverable again with its failure counter reset.
8. The delivery reconciler never redelivers a delivery whose id has a
   stored row.

**Serialisation and credential operations**

9. At most one unit of work holds the lease for a given `(tenant,
   adapter, lease key)`; the holder identity is per unit of work, so two
   tasks in one process, or a redelivery to the same consumer, cannot both
   hold it.
10. Lease fencing is checked atomically with every guarded write,
    including lease expiry, in the same statement as the write, so a holder
    whose lease expired or was taken over cannot commit even if it has not
    noticed; renewing or releasing a lease never changes the fence its
    holder keeps.
11. Every adapter call is bounded by a timeout shorter than the lease TTL,
    and an adapter with `credential_limit` never has more credentials
    (current, previous and in flight) than the limit under concurrent
    workers.

**Schema, startup and health**

12. The runtime role can read the schema-compatibility table (invariant
    13) the gate depends on, and a test running as that role passes the gate.
13. The schema gate compares the database's schema-compatibility integer
    (a single-row table written by every migration) with the integer the
    running code requires: it accepts a database whose integer is equal or
    greater (rolled-back code against a newer schema) and rejects a lower
    one, without needing the database's revision id to be known to the
    code.
14. While the schema is not current, the api and the long-running
    processes stay alive (liveness passes) and report not ready, and they
    exit non-zero only after `SCHEMA_WAIT_SECONDS`; waiting never fails
    liveness.
15. With `MODE=mock` no process waits for a schema or touches a database
    for it.
16. Entering the lifespan more than once in a process (tests, reloads)
    registers each trustbridge health check exactly once on the template's
    registry.
17. A process refuses to start in production with
    `RUN_MIGRATIONS_ON_STARTUP=true`, and no runtime process executes a
    migration.
18. The migrate Job runs as the owner role, two concurrent Job runs do not
    interleave, and a failed migration fails the Job so the rollout does
    not start.
19. A near-miss environment variable name (for example
    `LEASE_TTL_SECOND`) fails startup, and the effective value of every
    trustbridge setting that has a default is logged at startup.
20. A smoke test enters `trustbridge.main:app`'s lifespan, asserts the
    startup checks ran and that the webhook route exists, and fails if the
    template stops exposing `app.router.lifespan_context`.

**Operations, secrets and names** (the adapter side is in ADR 0029)

21. CI outputs are recoverable after a crash at any point between a
    platform change succeeding and the GitHub writes being recorded as
    done: a retry writes the outputs again, for issued secrets from the
    parked values, and for OIDC trust and for `UNCHANGED` by re-deriving
    them, so no scope is left with platform state but no GitHub outputs.
22. The orphan sweep never deletes a credential created after its
    database read: it lists the platform first, reads the database after,
    and deletes only credentials that appear in the list and are
    unaccounted for in that read.
23. Parked secret values are removed on every terminal operation state
    (finished, failed, abandoned), and a sweep removes values whose
    operation is terminal or missing, including after a crash.
24. A platform-side credential name contains the full tenant id, and an
    adapter that shortens a name shortens only the scope part.

**Abuse limits**

25. A webhook request whose source address is over
    `WEBHOOK_RATE_LIMIT_REQUESTS` in the current
    `WEBHOOK_RATE_LIMIT_WINDOW_SECONDS` gets 429 with `Retry-After` set to
    the seconds left in that window (at least 1) without any of its body being read, requests with an invalid signature
    count toward the limit, and an address in
    `WEBHOOK_RATE_LIMIT_EXEMPT_CIDRS` is never limited.
26. The limiter keys on `request.client.host` only and never parses
    `X-Forwarded-For` itself: behind `ProxyHeadersMiddleware` with
    `TRUSTED_PROXIES`, a header sent by a peer outside the list does not
    change the key, with a trusted chain the right-most untrusted entry is
    the key, an IPv6 address is keyed by its /64, a missing client keys as
    `unknown`, and production startup refuses `TRUSTED_PROXIES=*` or an
    unset `TRUSTED_PROXIES`.
27. Two api instances using one Valkey share each address's count.
28. When Valkey errors or exceeds `WEBHOOK_RATE_LIMIT_TIMEOUT_MS`, the
    limiter lets the request through to normal handling (a valid delivery
    still gets its 2xx after the commit), emits its metric, and adds at
    most that timeout in total to the request.

**Host availability** (the model is in ADR 0030)

29. A webhook for a host that is `unregistered` gets 503 with its body
    unread, and a webhook for a host that is `unavailable` (not
    `unregistered`) is verified, stored and answered 2xx like any other
    (0030 invariants 7-11).

## Decisions (G-api, 2026-11-01)

Outcomes of the user's API gate. Items 1-9 are approved as proposed; item
10 was decided differently from the lean.

1. **Ingest as a transactional outbox**: approved as written.
2. **Tenant identity**, a surrogate tenant UUID for `(host,
   installation_id)`: approved.
3. **Per-host webhook route** `/webhooks/github/{host}`: approved.
4. **Serialisation by a lease row with fencing**: approved.
5. **Write-ahead credential operations with resumable rotation**
   (contract in ADR 0029): approved.
6. **Dedicated migrate job, no startup migration in production, owner and
   application roles, schema gate**: approved.
7. **Wrapping the template's lifespan** through
   `app.router.lifespan_context`, guarded by the smoke test: approved. The
   wrapper's failure policy is the per-host availability model decided in
   ADR 0030 item 5: only invalid settings stop a process (see "Lifespan").
8. **Settings hygiene**: approved.
9. **Retention and tuning defaults**: approved as starting points, all
   settings: body retention 7 days after processing and 30 days after
   dead-lettering, delivery id retention 30 days, lease TTL 120 s, adapter
   call timeout 30 s, relay recheck 15 min, schema wait 300 s
   (`SCHEMA_WAIT_SECONDS`), host recheck 60 s (`HOST_RECHECK_SECONDS`, new
   with the availability model), `MAX_DELIVERY_ATTEMPTS` and the retry
   backoff bounds set by NG3.
10. **Abuse limits: option (b), an application limiter**, not the ingress
    controller (the lean was (a)). Per source address, Valkey-backed and
    shared across replicas, fail open. Specified in "Abuse limits on the
    webhook route", with invariants 25-28 and the settings there as
    proposed defaults. NG3 owns it; NG6 owns only the admin API's own
    limits.

Judgement calls made while recording these (not user decisions; revisit if
wrong):

- The limiter uses a fixed window, a 300-per-60-s default, a /64 key for
  IPv6, a 50 ms Valkey timeout and an exempt-CIDR list, all proposals.
- Trusted proxies are configured once, as `TRUSTED_PROXIES`, applied
  through uvicorn's `ProxyHeadersMiddleware`; `*` and an unset value are
  refused in production.
- A request that fails the signature check still counts toward the limit.

## Open issues

- The schema-compatibility table's name and column are NG3's to choose.

- ADR 0031 must settle RLS for `webhook_deliveries` (host-scoped, written
  before the tenant is known) and how the relay and reconciler read across
  tenants without a `BYPASSRLS` role (proposed: iterate tenants, setting
  the value per tenant; the relay reads only the host-scoped table).
- The operation value store (where parked secret values live between
  issue and delivery) is specified in NG3; ADR 0029 fixes only its
  contract (also listed there).
- The 25 MiB payload cap is GitHub's documented limit as recalled for
  this ADR and was not re-fetched in this pass; NG3 confirms it.
- `lifespan_context` is not a documented Starlette extension point; the
  smoke test (invariant 20) is the guard.
- The limiter's defaults (300 requests per 60 s per address) are untested
  against real GitHub and GHES delivery rates; NG3 confirms them, and GitHub's
  published hook ranges (`GET /meta`) are available for the exempt list.
- While Valkey is down the limiter does nothing and each request can still
  buffer up to `WEBHOOK_MAX_BODY_BYTES` before the signature check. A
  per-process fallback limiter (an in-memory token bucket) would bound
  that; it was not part of the decision (fail open) and is NG3's to
  propose.
- Fixed windows allow up to twice the limit across a window boundary;
  accepted for an abuse limit, not a quota.
- Behind a proxy that does not set `X-Forwarded-For`, all clients share the
  proxy's address and the limiter throttles them together; the deployment
  must configure the proxy, which the Helm chart (NG7) documents.
- Where a webhook secret is read from (the secret store) is outside the
  limiter; see 0030 invariant 11 for a secret-store outage.
- Host state is held per process (api replica, worker, reconciler); the
  admin API shows the answering replica's view. A shared source of truth
  (for example a table) would remove the disagreement and is NG3's to
  propose if the views diverge in practice.

## Consequences

- Instance code is safe from sync with no manifest entries, and the
  template's own tests, N-1 job and lint keep covering the schema.
- The cost: two packages, a wrapper entrypoint, and an entrypoint
  override at deploy time. A released image run without the override
  serves the template app. Documented; the Helm chart makes the override
  explicit.
- If the template changes `app.main` in a way that stops
  `from app.main import app` or `app.router.lifespan_context` working
  (renamed variable, factory), the entrypoint breaks. The smoke test
  makes a sync PR fail visibly.
- Mutating the template's `app` after import is simple but couples us to
  its router order and middleware stack; accepted.
- Hand-written revisions plus a wrapper CLI is a convention people can
  forget; the drift test is the backstop.
- At-least-once processing needs every handler to be idempotent; ADR 0029
  makes that an interface rule.
- Storing bodies adds a retention duty (EB-15) and database size
  proportional to webhook volume; the purge bounds it.
- Because the stream is trimmed only below what the group acknowledged, a
  stalled consumer group lets the stream grow by one id per event; the
  lag metric (invariant 6) is the alarm.
- Four process types and a migrate hook make a fuller Helm chart (NG7).
- The invariant list is the acceptance bar for NG3; a design that meets
  it differently from what the earlier drafts described is acceptable.

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
- redis-py streams (`xadd`, `xreadgroup`, `xautoclaim`, `xtrim`):
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
