# github/

The GitHub core ([ADR 0028](../../../docs/adrs/0028-trustbridge-architecture-and-instance-layout.md),
[0030](../../../docs/adrs/0030-key-signer-and-github-hosts.md)), built on githubkit.

- `hosts.py` — `GitHubHostConfig` (the `GITHUB_HOSTS` schema), the loader (kind-dependent
  `oidc_id_claims` default) and `validate_hosts`, the fatal settings checks.
- `availability.py` — `HostMonitor`: per-host `unregistered` / `unavailable` / `ready`, the signer
  self-test, the OIDC discovery check, and the `HOST_RECHECK_SECONDS` loop. Only invalid settings
  stop a process; everything that needs the signer, network or GitHub marks one host unavailable.
  The recheck loop also re-probes a ready host whose effective config (stored registration) or
  signer key id changed, so a rotation needs no restart.
- `app_jwt.py`, `tokens.py` — own JWT assembly through a signer (`exp` <= 9 min, `iat` 60 s back);
  installation tokens cached until ~5 min before expiry and down-scoped per repository and
  permission. `invalidate` also discards a mint that was in flight and re-mints once.
- `client.py`, `gateway.py` — a thin githubkit facade (token auth strategy per call; the host's CA
  bundle and base URL).
- `writer.py` — libsodium sealed-box secrets, variables and environments; the environment
  protection reader the OIDC eligibility rule uses ("protected" = at least one entry in
  `protection_rules`).
- `environments.py` — `SqlEnvironmentStore` over `github_environments` (migration `9a4d6c2e8b17`, NG6h-6a):
  one row per environment trustbridge itself created, which a tenant deletion removes (ADR 0034
  decision B6) and no other. `GitHubOutputWriter(gateway, environments)` reads the environment
  first (`GET`, `administration: read`): 404 means the following `PUT` creates it, and the row is
  written **after** the `PUT`; any other answer (the environment exists) records nothing. A crash
  between the `PUT` and the row, or a failed row write (a retryable `OutputError`; the retry finds the
  environment and records nothing), leaves a created but untracked environment, which is kept: the
  failure direction is always "not deleted". A repeat creation of an already tracked environment
  refreshes `created_at`. Without a tracker (the previous release) the writer does the `PUT` alone and
  records nothing, so N-1 writers leave environments untracked and therefore kept. The table is
  tenant-scoped (`tenant_isolation`, inert until release N+1 like the other tenant tables); the app role
  has `SELECT, INSERT, DELETE` and `UPDATE (created_at)` only. `tracked(tenant_id)`,
  `tracked_since(tenant_id)` (the same with `created_at`, for the recreation guard) and
  `forget(tenant_id, repository_id, name)` are the GitHub deletion step's calls (the `TrackedEnvironments` port in
  `deletion/ports.py`; the packages do not import each other). The database step also clears a
  tenant's rows. Both composition roots (`admin/credentials.py`, `worker/tenant_reconcile.py`) pass the store
  to the writer. `github_environments` is also in `tenancy.role_check.RLS_TABLES`.
- `checks.py`, `deployments.py` — check runs (scan results, manual-action notes) and deployments
  with statuses from Argo CD notifications. A static-header notification is only a trigger: state
  is re-read through the Argo CD adapter (`DeploymentStateSource`).
- `manifest.py` — the manifest flow for trustbridge's own App and the per-tenant read-only Argo CD
  App, and the manual fallback. `models.py` stores a host's registration for every process.
- `handlers.py` — the handler registry the worker dispatches through, and the installation
  lifecycle handlers. GitHub does not order deliveries, so `created`/`deleted`/`suspend`/
  `unsuspend` are triggers: `installations.py` (`AppInstallationLookup`) re-reads the
  installation with the App JWT (404 = gone) and that state is applied. `webhook_secrets.py` caches webhook secrets (0030 invariant 11) and
  re-reads one after a signature mismatch (rotation). An event for a tenant that is deleted,
  being deleted or tombstoned (ADR 0034; `TenantDeletedError` from the store) is acknowledged and
  dropped with an info log: no repository write, no reconcile, no audit event, cached tokens dropped.
- `hook_deliveries.py` — `GET /app/hook/deliveries` (cursor pagination) and redelivery, for the
  reconciler.
- `runtime.py` — `build_host_runtime(settings, deps)`: signers, `HostMonitor`, tokens, gateway,
  hook client, `HandlerRegistry` (core handlers registered) and the Argo CD handler.
- `ports.py` — the Protocols this package needs from the rest of the app (secret store, tenants,
  nonces, reconcile scheduling); `trustbridge.worker.ports` implements them.

## Registering an App

Default: the admin UI starts the manifest flow (`ManifestFlow.start`), the browser POSTs the
manifest to GitHub, GitHub redirects to `CALLBACK_PATH` with a `code`, and `ManifestFlow.complete`
exchanges it. The PEM goes straight to the host's signer (`KeyImporter`: Transit BYOK import for
`vault-transit`, a 0600 file for `file`) and is never logged or returned to the browser.

GHES 3.18 enterprise-owned Apps have no manifest flow. `ManifestFlow.manual_checklist` generates
the steps (webhook URL, events, permissions); the admin then submits the App id, PEM and webhook
secret to an authenticated endpoint that calls `register_manually`, which stores them identically.

## Unconfirmed against a live instance

GHES and GHE.com OIDC issuer values and whether their tokens carry `repository_id` /
`repository_owner_id` are not confirmed (no network in this item); `oidc_id_claims` stays false by
default there and the issuer is always configuration. GitHub documents no webhook event for an
environment's protection rules changing, so eligibility is re-evaluated at each reconcile.
