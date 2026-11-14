# trustbridge/worker/

The runtime core of trustbridge, shared by the api, the worker and the reconciler
([ADR 0028](../../../docs/adrs/0028-trustbridge-architecture-and-instance-layout.md),
[0030](../../../docs/adrs/0030-key-signer-and-github-hosts.md)). Import order inside
`trustbridge` is `main` > `webhooks` > `worker` > `settings` (import-linter, `pyproject.toml`);
`trustbridge.github` sits beside these layers (see `../README.md`).

| Module | What it is |
| --- | --- |
| `models.py` | Tables on the template's shared `Base`: `webhook_deliveries`, `scope_leases`, `tenants` (with `org`, `org_id`, `state`), `tenant_repositories`, `schema_compat`. The revisions `c3a5e7d91f42` and `5d1f3a7c9e24` in `alembic/versions/` are the schema of record. |
| `deliveries.py` | `DeliveryStore` (Protocol), `PgDeliveryStore`, `InMemoryDeliveryStore` (mock mode and unit tests), the relay rules (`is_relayable`) and the retry backoff. `mark_processed`, `record_failure` and `mark_dead` take the `attempts` the worker claimed and update only a row still `pending` with that counter (False or 0 on a lost claim; the worker logs it, acks, and, for `dead`, skips the dead letter). A requeue back to the same counter value (0) is a known residual: NG6g decides on a generation column and its migration. `requeue_statement` is the one reset the admin API also runs in its transaction. |
| `queue.py` | `StreamQueue`: `tb:events` (`{id, host}` keys only), `tb:events:dead` (`{host, delivery_id, error}`), consumer group `tb-workers`, safe trimming, lag. redis-py `redis.asyncio` against Valkey (ADR 0013). |
| `relay.py` | Adds delivery ids from the outbox to the stream (tick, re-relay sweep, host-ready). |
| `worker.py` | The consumer; dispatches through the GitHub core's `HandlerRegistry` (`EventContext`). |
| `reconcile_queue.py` | `ReconcileQueue`: claim/ack/extend over the reconcile requests. `claim` moves a tenant id from `tb:reconcile` to `tb:reconcile:inflight` and stamps it in the hash `tb:reconcile:claims` (claim time and a token) in one Lua script, as do `ack`, `release` and `reclaim` (token or listed-entry compare plus the set and hash change in one step, so a stale holder cannot undo a newer claim); `ack` removes it, and returns False when the claim was reclaimed; `release` puts it back; `extend` re-stamps a running claim (same token check, one script); `begin_attempt` / `clear_attempts` count the runs of a request in `tb:reconcile:attempts`; `reclaim` returns requests inflight longer than `TENANT_RECONCILE_CLAIM_TTL_SECONDS` to pending (a member with no stamp is stamped first, so a slow claimer is not undone). A crashed or failed run therefore never loses a request; the retry comes after the TTL. A run starts no body after half the TTL; its remaining claims go back to pending. The TTL is at least `LEASE_TTL_SECONDS`, so a reconcile holding a scope lease is not reclaimed under it. A running body extends its claim before each write step, so only a holder that stopped heartbeating is reclaimed. A reconcile run twice is detected at the second ack and must be idempotent (ADR 0029). |
| `reconciler.py` | The reconciler process: relay tick, sweep, delivery reconciliation (`HookDeliveryClient`), retention, the audit purge (`audit-purge`, a singleton under a lease, bounded per run, a heartbeat per batch), tenant deletion (`tenant-deletion`, a singleton under a lease that polls a `DeletionDriver` and imports only `trustbridge.deletion.ports`; a store error from a pass is logged and ends that poll, a heartbeat per pass), trimming, and the `tenant-reconcile` duty (replica-safe, no lease: drains `ReconcileQueue`; a request for a disabled, deleting or gone tenant is acknowledged and dropped with a log line, ADR 0033 invariant 19; any other request runs the `TenantReconciler` body when `TENANT_RECONCILE_ENABLED` is true, and stays pending otherwise; the body receives an `extend` callable that re-stamps its claim and beats the loop, and `ClaimLostError` from it aborts the body, logged, before its next write step; a body that has run `TENANT_RECONCILE_MAX_ATTEMPTS` times for a tenant is dropped with an ERROR line). |
| `tenant_reconcile.py` | The reconcile body (`CredentialReconciler`) and its factory `build_tenant_reconciler`, which composes a `CredentialCore` for the reconciler as `admin/credentials.py` does (enabled adapter classes through the read-only registry, `SqlCredentialStore` with the audit observer, actor `reconciler`, `ParkedValues`, the GitHub writer and inspector, `SqlTenantGate`). For one tenant: resume its open operations; `ensure_scope` for each stored `adapter_credentials` row with the tenant's stored scopes as siblings (this re-evaluates OIDC eligibility, ADR 0029 A12); `sweep_orphans` for each adapter it has credentials for; `sweep_parked_values` (global, so at most once a minute per process). It creates no new scope, is idempotent, and writes no audit event of its own (the store observer audits each credential change). A step that fails (platform, GitHub, lease) does not stop the others; the body then raises `ReconcileIncompleteError` and the request is retried after the claim TTL. A tenant disabled mid-run stops the body quietly. |
| `platform_deletion.py` | The deletion engine's platform port (NG6h-5): `SqlPlatformPort` lists a tenant's recorded credentials and resources and removes one at a time through `CredentialCore.delete_scope(..., deletion_id=, remove_outputs=False)` and `ResourceCore.delete`; `ConnectionNames` lists the adapter names that have connection data in the secret store (names only). `build_platform_port` (in `tenant_reconcile.py`) composes it. Nothing composes it into a process until NG6h-7. |
| `github_deletion.py` | The deletion engine's GitHub port (NG6h-6): `SqlGithubPort` lists the secrets and variables recorded in a tenant's credential rows and its tracked environments, removes them through `GitHubOutputWriter.remove` and the tenant gateway (clearing each from its row), reads an environment's `created_at`, and checks the installation with the App JWT. `build_github_port` (in `tenant_reconcile.py`) composes it for the NG6h-7 root. |
| `lease.py` | Fenced scope leases (`LeaseManager`, `guarded_execute`, `guarded_insert`, `bounded_call`); `LeaseManager` and `Lease.guard()` are the adapter core's `LeaseProvider` and `Lease`. |
| `secrets.py` | `LocalSecrets` (`file:` / `env:` refs to read, `store:` refs under `SECRET_STORE_DIR` to read, write, delete and list) and its in-memory twin; the `SecretReader` / `SecretWriter` of the GitHub core. |
| `ports.py` | The ports of the GitHub and adapter cores over these components: `SqlTenants` / `MemoryTenants` (installation store, installation resolver, tenant resolver; `set_installation_state` and `active_tenant_id_for` refuse a deleting, `deleted` or tombstoned tenant with `TenantDeletedError`, the former under the same per-installation advisory lock as `upsert_installation`), `ParkedValues` (value store, in the secret store), `ConnectionData` (tenant connection store, config resolver), `ReconcileRequests` (the Valkey set `tb:reconcile`; the api records admin reconcile requests in it too, and `ReconcileQueue` consumes it, NG5r). |
| `schema.py` | The schema gate (`schema_compat` integer) and its readiness check. |
| `health.py` | `/health/live` and `/health/ready` for the worker and reconciler on `HEALTH_PORT` (standard-library asyncio, no dependency), and the `Heartbeat` the loops report to. See `docs/install.md`. |
| `runtime.py` | `build_core` (stores, ports, GitHub runtime) and the bootstrap of the worker and reconciler commands (`monitor.check_all` / `start` / `stop`). |
| `migrate.py` | The migrate Job and the autogenerate wrapper. |
| `metrics.py` | OpenTelemetry instruments (no-ops without a MeterProvider). |

## Commands

One image, different commands (the image's default entrypoint, `runner.sh`, serves the
template app: deployments override it).

```bash
uvicorn trustbridge.main:app --no-proxy-headers --host 0.0.0.0 --port 8000   # api
python -m trustbridge.worker worker                                          # stream consumer
python -m trustbridge.worker reconciler                                      # relay, reconciler, retention
python -m trustbridge.worker migrate upgrade                                 # the migrate Job (owner role)
python -m trustbridge.worker migrate revision --autogenerate -m "..."        # autogenerate wrapper
```

`--no-proxy-headers` is required for the api: `TRUSTED_PROXIES` (applied through
`ProxyHeadersMiddleware` in `trustbridge.main`) is then the only trusted list.
ADR 0028 names the commands `python -m trustbridge.reconciler` and
`python -m trustbridge.migrations upgrade`; they are subcommands here because this item's write
set is `worker/`. A one-line `__main__` shim in those packages can restore the ADR names.

No process except `migrate` runs a migration. Production refuses `RUN_MIGRATIONS_ON_STARTUP=true`.

## Settings added by trustbridge

All from the environment (`TrustbridgeSettings`, `../settings.py`); no dotenv file. Defaults
for retention, leases and timeouts are ADR 0028 decision 9. `GITHUB_HOSTS` and `SIGNERS` have
no default. A near-miss variable name (for example `LEASE_TTL_SECOND`) fails startup; the
effective defaults are logged at startup.

| Variable | Default | Meaning |
| --- | --- | --- |
| `GITHUB_HOSTS`, `SIGNERS` | none (required) | JSON; ADR 0030. |
| `TRUSTED_PROXIES` | unset (refused in production) | Comma list or JSON list of proxy IPs/CIDRs. `*` is refused in production; `[]` means no proxy. |
| `WEBHOOK_MAX_BODY_BYTES` | 26214400 | Body cap (GitHub's documented 25 MB payload cap). |
| `WEBHOOK_RATE_LIMIT_ENABLED` / `_REQUESTS` / `_WINDOW_SECONDS` / `_TIMEOUT_MS` | true / 300 / 60 / 50 | The limiter. |
| `WEBHOOK_RATE_LIMIT_EXEMPT_CIDRS` | `[]` | JSON list of CIDRs that bypass the limiter. |
| `MAX_DELIVERY_ATTEMPTS` | 8 | Handler failures before the dead letter, and the stream delivery count that dead-letters a delivery that keeps ending the worker. |
| `RETRY_BACKOFF_BASE_SECONDS` / `_MAX_SECONDS` | 30 / 1800 | Retry delay `base * 2^(failures-1)`, capped. With 8 attempts a delivery is retried for about an hour (30, 60, 120, 240, 480, 960, 1800 s) before it is dead-lettered; a longer platform outage needs a requeue. |
| `BUSY_RETRY_SECONDS` | 10 | Delay after a busy lease (not a failure). |
| `DISABLED_TENANT_RETRY_SECONDS` | 15 | Delay before a disabled tenant's delivery is checked again (not a failure). |
| `PUBLIC_BASE_URL` | unset | Public base URL of the deployment, used in the GitHub App manifest (webhook URL and the redirect URL the BFF serves). An absolute https origin (http only for localhost, 127.0.0.1, ::1), no credentials, path, query or fragment; the trailing slash is dropped; a bad value stops the load. Unset: host registration is a 503 (ADR 0030). |
| `REGISTRATION_STATE_KEY` | unset | At least 32 characters, shared by every api replica; signs the manifest flow's one-time `state`. Unset: host registration is a 503. |
| `STREAM_CLAIM_IDLE_SECONDS` | 120 | A pending entry idle this long is claimed from a dead consumer. Keep it above the longest handler run (the lease TTL). |
| `LEASE_TTL_SECONDS` / `ADAPTER_CALL_TIMEOUT_SECONDS` | 120 / 30 | The timeout must be shorter than the TTL (validated). |
| `BODY_RETENTION_PROCESSED_DAYS` / `_DEAD_DAYS` / `DELIVERY_ID_RETENTION_DAYS` | 7 / 30 / 30 | Bodies are purged after these; the row stays until the id retention elapses. |
| `RELAY_RECHECK_SECONDS` | 900 | The re-relay sweep. |
| `AUDIT_PURGE_INTERVAL_SECONDS` / `AUDIT_PURGE_BATCH_SIZE` / `AUDIT_PURGE_MAX_BATCHES` | 3600 / 1000 / 100 | The reconciler's audit purge: run interval, rows per transaction (1 to 10000) and batches per run (1 to 10000; a run also stops starting batches after half `LEASE_TTL_SECONDS`). Retention itself is `AUDIT_RETENTION_DAYS` (audit README). |
| `TENANT_RECONCILE_ENABLED` / `_INTERVAL_SECONDS` / `_BATCH_SIZE` / `_CLAIM_TTL_SECONDS` / `_MAX_ATTEMPTS` | true / 30 / 50 / 300 / 5 | The reconciler's tenant reconcile duty: whether the reconcile body runs (false: requests for a reconcilable tenant stay pending), the poll interval, the claims per poll, the age after which an unextended, unacknowledged claim is returned to pending (at least `LEASE_TTL_SECONDS`), and the runs after which a request that keeps failing is dropped with an ERROR log (1 to 100; each retry waits one claim TTL). |
| `DELETION_POLL_SECONDS` / `DELETION_BATCH_SIZE` | 10 / 10 | The reconciler's tenant deletion duty (ADR 0034): the poll interval and the deletions claimed per poll (1 to 1000). With no `DeletionDriver` composed (until the steps of NG6h-4b, -5 and -6 exist) the duty does nothing. |
| `DELETION_LEASE_TTL_SECONDS` / `DELETION_BUDGET_FRACTION` | 120 / 0.5 | The fenced database lease of one deletion (1 to 3600 s, separate from `LEASE_TTL_SECONDS`) and the share of it after which a run releases the lease and stops (strictly between 0 and 1). |
| `DELETION_BACKOFF_BASE_SECONDS` / `_CAP_SECONDS` | 30 / 3600 | A failed attempt waits `min(cap, base * 2^attempts)` seconds, at most 86400 each; base must not exceed cap. |
| `DELETION_DRAIN_MAX_PASSES` / `DELETION_DRAIN_POLL_SECONDS` | 30 / 2 | The drain step re-scans the tenant's writers every poll, at most this many passes (at least 2). |
| `DELETION_STEP_PASS_SECONDS` | 30 | The longest one step pass may take between two lease renewals; steps size their batches to it. `DELETION_DRAIN_POLL_SECONDS + DELETION_STEP_PASS_SECONDS` must be at most half `DELETION_LEASE_TTL_SECONDS`: refused at startup in production, a warning elsewhere. The `tenant-deletion` loop is stale after its interval plus the longer of the two lease TTLs. |
| `SCHEMA_WAIT_SECONDS` | 300 | How long a process waits for the schema before it exits non-zero. |
| `HEALTH_PORT` | unset (no endpoint) | Port of the worker's and reconciler's health endpoints, 1 to 65535. The api ignores it (it serves `/health/*` on its own port). A loop is stale after its interval plus `LEASE_TTL_SECONDS` without progress (each finished entry or job step counts). Readiness is evaluated one at a time and cached for 2 s. |
| `HOST_RECHECK_SECONDS` | 60 | Recheck of `unavailable` and `unregistered` hosts. |
| `MIGRATE_EXPECTED_ROLE` | unset | The migrate Job refuses to run as any other database role. |

## Interfaces for the adapters (NG5) and the admin API (NG6)

- `lease.py`: `LeaseManager(PgLeaseStore(session_factory), ttl_seconds=...)`; `hold(tenant_id=...,
  adapter=..., key=...)` and `Lease.guard()` are what `CredentialCore` uses (`LeaseProvider`,
  `Lease`). Docstring at the top of the module.
- `../settings.py`: `TrustbridgeSettings`, `get_trustbridge_settings()`, `GitHubHostConfig`
  (`trustbridge.github.hosts`), `SignerConfig` (`trustbridge.signer.config`).
- `runtime.py`: `build_core(...)` returns a `Core` with the stores, the `HostRuntime`
  (`trustbridge.github.runtime`), the secret store, `ParkedValues` (the credential core's value
  store) and `ConnectionData` (its config resolver and the tenant connection store). The web
  processes override pieces through `main.Overrides`.
- `SECRET_STORE_DIR` (unset by default): the directory behind `store:` secret references (parked
  values, tenant connection data). Without it every `store:` write fails closed. Vault KV or a
  cloud secret manager implement the same four methods (`read`, `write`, `delete`,
  `list_refs`) in `secrets.py`'s place.
- `worker.py`: handlers (registered in the `HandlerRegistry`) must be idempotent; raise `LeaseBusy` for a taken lease (no failure is
  counted), any other exception is a counted failure. A delivery whose payload names an installation
  of an admin-disabled tenant is deferred by `DISABLED_TENANT_RETRY_SECONDS` (default 15) and
  acknowledged, without a counted failure (`TenantDisabled`, and `SqlTenantGate` in `tenancy/gate.py`; ADR 0033).
  Before that decision the worker stores the tenant's id on the delivery row
  (`DeliveryStore.set_tenant`, `webhook_deliveries.tenant_id`; ADR 0034: a deletion must find every
  delivery of its tenant), using `TenantGate.standing` (`TenantStanding`, defined in `tenancy.standing`: id, disabled flag, and a
  `deleting` or `tombstone` refusal). A tenant with a deletion in progress is deferred without a counted failure too (`TenantDeleting`, distinct
  from `TenantDisabled`) but four times as long as a disabled one (`DELETING_RETRY_FACTOR`), since `enable` does not end
  it; it is logged at info the first time a delivery is deferred for it and at debug after that;
  a tombstoned installation (a finished deletion, the App still installed) has its delivery
  acknowledged and marked processed with no handler run and no tenant created, and a log line.
- Migrations: bump the schema-compatibility integer in every revision with
  `INSERT INTO schema_compat (id, version) VALUES (1, N) ON CONFLICT (id) DO UPDATE SET version =
  GREATEST(schema_compat.version, N)` and raise `REQUIRED_SCHEMA_VERSION` in `schema.py` when the
  code needs it. A revision that adds a table grants the runtime role (see `grant_runtime_access`
  in the NG3 revision).

## Don't

- Import `trustbridge.main` or `trustbridge.webhooks` from here (import-linter enforces it).
- Run a migration from a runtime process, or hold an advisory lock for a lease: the lease is a
  row, because the hold outlives a transaction and PgBouncer transaction mode drops session locks.
- Put a body in the stream or the dead letter: they carry the delivery id (and an error) only.
