# 0028. Architecture of the trustbridge GitHub App, and where instance code lives

## Status

Proposed. Builds on [0013](0013-valkey-over-redis-licensing.md) (Valkey
server, redis-py client), [0026](0026-resource-server-metadata-and-bff.md)
(resource-server validation, which the admin UI's BFF relies on) and
[0027](0027-product-analytics-with-track.md); all three are inherited from
template-fastapi. Companion ADRs: [0029](0029-adapter-plugin-interface.md)
(adapter plugin interface) and
[0030](0030-key-signer-and-github-hosts.md) (key signer and GitHub hosts).
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

## Decision

### Layout: an instance package beside `app`

- Instance code lives in **`src/trustbridge/`**, a sibling package of the
  template's `src/app/`. Sub-packages: `webhooks/`, `worker/`, `github/`,
  `adapters/<name>/`, `tenancy/`, `audit/`, `rbac/`, `models/`,
  `settings.py`, `main.py`. This is the standing design. A template hook
  (for example an entry-point for routers, or an `APP_MODULE` variable in
  `runner.sh`) would be a possible future simplification, not a
  prerequisite.
- `pyproject.toml` (merge tier) gets `packages = ["src/app",
  "src/trustbridge"]` and importlinter `root_packages = ["app",
  "trustbridge"]` plus a contract that `app` never imports `trustbridge`.
  `trustbridge` may import `app`.
- **Entry point `trustbridge.main:app`** imports `app.main:app` (the
  template app: health, auth, problem details, security headers,
  telemetry, lifespan) and mounts trustbridge's routers on it. Nothing in
  `src/app/` is edited.
- **Settings:** `trustbridge.settings.TrustbridgeSettings` subclasses
  `app.config.Settings` and adds the GitHub host, signer, queue and
  adapter fields. Both classes read the same environment: the template
  keeps `get_settings()` (base fields), trustbridge code uses
  `get_trustbridge_settings()`. `Settings` uses `extra="forbid"`
  (`src/app/config.py:41`), which only rejects unknown init kwargs, not
  unrelated environment variables (tested in the template's
  `tests/unit/test_config.py`), so the two coexist.
- **Process commands.** `scripts/runner.sh:16` hardcodes `app.main:app`
  and the `Dockerfile` (replace tier, `ENTRYPOINT ["runner.sh"]`, line
  112) cannot be edited. The image therefore contains both packages
  (`COPY src ./src`, `Dockerfile:50`) but its default entrypoint serves
  the template app. Deployments override the entrypoint:
  api = `uvicorn trustbridge.main:app ...`, worker =
  `python -m trustbridge.worker`, reconciler = `python -m
  trustbridge.reconciler`. The Helm chart (NG7) and the instance's
  compose override set `command`/`entrypoint` explicitly. A plain
  `docker run` of the image serves the template app (no webhooks); this
  is documented in the README. No local-manifest ignore entries are
  needed for any of this.

### Migrations: one history, extended through a thin wrapper

`alembic/env.py` (replace) builds `target_metadata` from
`craftainer_crud.models.base.Base` and imports only `app.models.hero`
(lines 12-19, 29). `alembic/versions/` is `ignore` tier, so instance
revisions are kept. `app.main._run_migrations` runs
`command.upgrade(Config("alembic.ini"), "head")` (`src/app/main.py:58`),
and `scripts/post-create.d/20-migrate.sh` runs `alembic upgrade head`.

We will keep **one linear history in `alembic/versions/`** and put
trustbridge's models on the same `Base`. Revisions are hand-reviewed
anyway; upgrading needs no metadata, so the template's `env.py`, startup
migration and post-create step apply them unchanged. For autogenerate we
add `python -m trustbridge.migrations revision --autogenerate`, a wrapper
that imports `trustbridge.models` (registering the tables on `Base`)
before calling Alembic's own CLI entry point, so the unmodified `env.py`
sees the full metadata. Plain `alembic revision --autogenerate` would see
trustbridge tables as extra and propose dropping them; two guards: a
unit test in `tests/` (instance-added files under replace-tier `tests/`
survive, finding 1) that runs `alembic.autogenerate.compare_metadata`
against a migrated database and fails on drift, and Squawk's
`ban-drop-table` in the existing lint.

Why not a second Alembic environment (own `versions` directory and
version table)? `.github/workflows/migration-compat.yml` triggers on
`alembic/versions/**` and `src/app/models/**` and
`scripts/lint-migrations.sh` takes `alembic/versions/` paths, both
template-owned (replace). A second history would escape the N-1
compatibility job and the Squawk lint, and the template's startup would
not apply it. With the single history both keep working. NG3 verifies
this in the integration lane (migrate a fresh database, run the template
suites) before NG3 merges.

### Request path and processing

1. **Ingress** `POST /webhooks/github` (GitHub documents: HTTPS, secret,
   signature validation, a 2xx within 10 s, process asynchronously,
   `X-GitHub-Delivery` for replay detection, redelivery after downtime):
   - Read the raw body and verify `X-Hub-Signature-256` with
     `hmac.compare_digest` before parsing JSON. The secret is per App
     (per GitHub host), never in the URL.
   - Insert the delivery ID into a `webhook_deliveries` table with a
     unique constraint (`INSERT ... ON CONFLICT DO NOTHING`). A conflict
     returns 2xx without enqueuing; redeliveries keep the original ID, so
     this also dedupes them.
   - `XADD` the verified payload reference to the Valkey Stream
     `tb:events`, then return 202. No GitHub or adapter call happens on
     the request path.
2. **Worker**: the same image, command `python -m trustbridge.worker`.
   Consumer group `tb-workers`; `XREADGROUP`, `XACK` after success;
   `XAUTOCLAIM` reclaims entries idle past a threshold from dead
   consumers; after N delivery attempts (tracked by the stream's
   delivery counter) the entry moves to the dead-letter stream
   `tb:events:dead` with the error, and is visible in the admin API.
   Handlers are idempotent (reconcile-style, ADR 0029), so at-least-once
   delivery is safe. Leader election is not needed for workers (the
   consumer group partitions work); the reconciler uses a Postgres
   advisory lock (EB-6).
3. **Delivery reconciler**: GitHub never redelivers on its own. A
   periodic job lists failed deliveries with the App-JWT endpoints
   (`GET /app/hook/deliveries`, `POST /app/hook/deliveries/{id}/attempts`)
   from the last cursor, redelivering those whose ID is missing from
   `webhook_deliveries`. This also covers downtime of the ingress.
4. **Installation tokens**: minted with the signer-backed App JWT
   (ADR 0030), cached in process until about 5 minutes before the
   1-hour expiry, and **down-scoped** per repository and permission set
   at mint time. githubkit's `AppInstallationAuthStrategy` handles
   minting and refresh; we wrap it so the JWT comes from the signer.

### Tenancy

Tenant = GitHub installation (EB-3). Every tenant-owned table has a
`tenant_id` and Postgres row-level security keyed on a session setting
(`SET LOCAL trustbridge.tenant_id`) applied per transaction by a
dependency. Details (policies, the owner/app role split, the audit
interplay) are ADR 0031. The worker sets the setting from the event's
installation ID before any query.

### Queue client: redis-py against a Valkey server

Follow ADR 0013: the server is Valkey, the client is `redis` (redis-py,
`redis.asyncio`), already pinned at `redis==8.1.0` and used by the
template's `app/cache.py`. trustbridge uses `redis.asyncio` for Streams
too (`xadd`, `xreadgroup`, `xautoclaim`, `xack`). Reasons: one client
and one connection configuration (`redis_url`, with the template's
`rediss://` enforcement) for both template and instance code; the stream
commands used are part of the shared protocol, which Valkey implements.
NG1 also added `valkey==6.2.0` (valkey-py), which nothing imports; **NG3
removes it**. If Valkey ever diverges from Redis protocol in a way that
matters, switching clients is a contained change behind
`trustbridge.queue`.

### Settings and hosts

All configuration is environment-driven through `TrustbridgeSettings`
(secrets from files or an external secret store, never in the image).
GitHub hosts are a list of host configurations (ADR 0030); adapters are
discovered through the `trustbridge.adapters` entry-point group and
enabled by name (ADR 0029). Telemetry and outbound calls other than to
configured hosts and adapters are off by default (EB-5).

## Consequences

- Instance code is safe from sync with no manifest entries, and the
  template's own tests, N-1 job and lint keep covering the schema.
- The cost: two packages, a wrapper entrypoint, and an entrypoint
  override at deploy time. A released image run without the override
  serves the template app. Documented; the Helm chart makes the override
  explicit.
- If the template changes `app.main` in a way that stops
  `from app.main import app` working (renamed variable, factory), the
  entrypoint breaks. The instance has its own smoke test that imports
  `trustbridge.main:app` and checks the webhook route, so a sync PR fails
  visibly.
- Mutating the template's `app` after import is simple but couples us to
  its router order and middleware stack; accepted.
- Hand-written revisions plus a wrapper CLI is a convention people can
  forget; the drift test is the backstop.
- At-least-once processing needs every handler to be idempotent; ADR 0029
  makes that an interface rule.
- Stream growth: `XADD ... MAXLEN ~` on `tb:events` and a retention
  setting for the dead-letter stream (EB-15).

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
- redis-py streams (`xadd`, `xreadgroup`, `xautoclaim`):
  https://redis.readthedocs.io/en/stable/commands.html
- Valkey stream commands:
  https://valkey.io/commands/#stream
- PostgreSQL row security:
  https://www.postgresql.org/docs/current/ddl-rowsecurity.html
- Alembic CLI and autogenerate: https://alembic.sqlalchemy.org/en/latest/autogenerate.html
