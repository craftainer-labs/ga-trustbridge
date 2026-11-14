# trustbridge/

The instance package of the trustbridge GitHub App, a sibling of the template's `src/app/`
([ADR 0028](../../docs/adrs/0028-trustbridge-architecture-and-instance-layout.md)). It may import
`app`; `app` never imports it (import-linter). Nothing under `src/app/` is edited.

- `main.py`: the api entrypoint `trustbridge.main:app`. Imports the template app, mounts the
  webhook routes and the admin API, installs `ProxyHeadersMiddleware`, wraps the template's lifespan (schema gate,
  host startup checks and recheck task, webhook secret cache) and releases its resources after
  the template has shut down.
- `settings.py`: `TrustbridgeSettings` (extends the template's `Settings`). `GITHUB_HOSTS` and
  `SIGNERS` are typed with the GitHub core's `GitHubHostConfig` and the signers' `SignerConfig`
  (one definition each); the settings-only host checks (`validate_hosts`) are fatal. Also
  near-miss detection and the startup logging of effective defaults.
- `webhooks/`: the GitHub webhook ingress, the Argo CD notification route and the abuse limiter.
  See its `README.md`.
- `admin/`: the admin API v1 read routes under `/admin/v1` (ADR 0033), their guards, rate limits,
  problem types and cursor. See its `README.md`.
- `ratelimit.py`: the fixed-window limiter (Valkey `INCR`, fail open) shared by `webhooks` and
  `admin`; a leaf module.
- `worker/`: the shared runtime core (outbox, stream, worker, reconciler, leases, schema gate,
  secret store, the ports over these for the GitHub and adapter cores, migrate Job). See its
  `README.md`.
- `github/`: the GitHub core (hosts, availability, tokens, writer, reporters, handler registry,
  `runtime.build_host_runtime`). See its `README.md`.
- `adapters/`: the adapter plugin interface (I2) and the credential core. See its `README.md`.
- `signer/`: the `file` and `vault-transit` App-key signers. See its `README.md`.
- `rbac/`: role-based access for the admin API (claims to grants, `require_role`). See its
  `README.md`.
- `audit/`: the append-only audit log (ADR 0032): storage (append, list, NDJSON export,
  retention purge), event validation, redaction and the emitters for the flows that exist. See
  its `README.md`.
- `tenancy/`: the per-session tenant context and the Postgres hook that sets it (ADR 0031). See its
  `README.md`.
- `deletion/`: the tenant deletion engine (ADR 0034): state machine, lease and fence loop, failure
  policy, drain step and audit events; the steps are Protocols filled by later items. See its
  `README.md`.
- `composition.py`: the composition root of the tenant deletion (NG6h-7a): builds the engine with its
  four steps and the record purger from the reconciler's `Core`. It is the one module that imports the
  concrete classes of both `deletion/` and `worker/`.
- `reconciler/`, `migrations/`: `python -m trustbridge.reconciler` runs the reconciler with the deletion
  composed (`composition.main`); `python -m trustbridge.migrations upgrade` is a one-line shim.

Import rules (import-linter, `pyproject.toml`): `main` > `webhooks` and `admin` (independent) >
`worker` > `ratelimit` > `settings`; `app` never imports `trustbridge`; `adapters` and `signer`
never import `github`; `audit` never imports `rbac` or `admin`; `rbac` never imports `tenancy`,
`admin`, `webhooks` or `main`; `deletion` imports only `tenancy`, `audit` and the template's `app`, nothing
below imports it, and the worker reaches it only through `deletion.ports`.

## How the pieces are wired

1. The api, the worker and the reconciler each call `worker.runtime.build_core`, which builds the
   stores and the ports over them (`worker/ports.py`) and passes those to
   `github.runtime.build_host_runtime`: signers, one `HostMonitor`, the token provider and
   gateway, the hook-delivery client and the `HandlerRegistry`.
2. Each process runs `monitor.check_all()` at startup, `monitor.start(HOST_RECHECK_SECONDS)` and
   `monitor.stop()` on shutdown. A failed check makes one host `unavailable`; only invalid
   settings (`HostSettingsError`, raised as a settings validation error) stop a process.
3. The worker dispatches each delivery through the `HandlerRegistry`; its monitor re-adds a
   host's waiting rows to the stream when the host turns `ready` (`MonitorDeps.on_ready`).
   Calls to GitHub go through `HostMonitor.require_ready`.
4. The ingress resolves the host's webhook secret with `WebhookSecretResolver.secret_for`
   (`HostUnregisteredError` and `WebhookSecretUnavailableError` are 503).
5. Argo CD notifications reach `/webhooks/argocd/{tenant_id}` (static bearer token per tenant) and
   call `ArgoCdDeploymentHandler.handle`; until a `DeploymentStateSource` exists (NG5d) the route
   answers 503.

Wired by the admin API (`admin/`, NG6e): the manifest flow (`github/manifest.py`) is mounted
under `/admin/v1/hosts/{host}/registration` (the BFF owns the browser callback), and the api builds
a `CredentialCore` for `POST .../credentials/{adapter}/{scope_key}/rotate` from the parts `Core`
holds (`admin/credentials.py`).

The reconciler builds a `CredentialCore` too (`worker/tenant_reconcile.py`, composed as the api does,
with the actor `reconciler`). The reconcile requests (`ReconcileScheduler`,
`POST /admin/v1/tenants/{t}/reconcile`) are recorded in the Valkey set `tb:reconcile`; the
reconciler's `tenant-reconcile` duty drains it with claim/ack (`worker/reconcile_queue.py`), drops a
request for a disabled, deleting or gone tenant, and otherwise runs the reconcile body for the
tenant (resume its open operations, re-evaluate each stored scope, sweep orphans and parked
values; it creates no new scope). `TENANT_RECONCILE_ENABLED` (default true) switches the body off:
the requests then stay pending. The background retry of an operation that failed mid-way is this
duty, driven by a request; no timer resumes operations on its own yet.

## Accepted behaviours

- The token cache's invalidation epoch is process-global: an invalidation for one installation
  makes any in-flight mint run again, which over-invalidates and is safe.
- A secret backend must raise `SecretNotFound` for an absent ref; a plain `SecretUnavailable`
  fails closed (`ConnectionData` does not treat it as an empty blob).
- A `deleted` installation event that arrives during a GitHub outage is retried and can
  dead-letter; the tenant stays active until the reconciler or a redelivery catches up.
- The migration's `RESET`s do not run if a statement fails first; the migrate job is a one-shot
  process, so the session settings die with it.
- Old pods insert with `ON CONFLICT (delivery_id)`, which fails once the primary key is
  `(host, delivery_id)` (migration `a7e2c94b1d63`); the migrate job runs before the rollout.
- Byte-identical webhook bodies on one host collapse to one delivery for the dedupe window; a
  replay under a new delivery id is answered `202 duplicate` and kept as a terminal marker row.

## Don't

- Edit template-owned files to hook in (the `Dockerfile`, `scripts/runner.sh`, `src/app/`);
  deployments override the entrypoint instead.
- Run migrations from the api, worker or reconciler.
