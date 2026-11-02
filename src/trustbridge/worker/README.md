# trustbridge/worker/

The runtime core of trustbridge, shared by the api, the worker and the reconciler
([ADR 0028](../../../docs/adrs/0028-trustbridge-architecture-and-instance-layout.md),
[0030](../../../docs/adrs/0030-key-signer-and-github-hosts.md)). Import order inside
`trustbridge` is `main` > `webhooks` > `worker` > `settings` (import-linter, `pyproject.toml`);
`trustbridge.github` sits beside these layers (see `../README.md`).

| Module | What it is |
| --- | --- |
| `models.py` | Tables on the template's shared `Base`: `webhook_deliveries`, `scope_leases`, `tenants` (with `org`, `org_id`, `state`), `tenant_repositories`, `schema_compat`. The revisions `c3a5e7d91f42` and `5d1f3a7c9e24` in `alembic/versions/` are the schema of record. |
| `deliveries.py` | `DeliveryStore` (Protocol), `PgDeliveryStore`, `InMemoryDeliveryStore` (mock mode and unit tests), the relay rules (`is_relayable`) and the retry backoff. |
| `queue.py` | `StreamQueue`: `tb:events` (ids only), `tb:events:dead` (`{delivery_id, error}`), consumer group `tb-workers`, safe trimming, lag. redis-py `redis.asyncio` against Valkey (ADR 0013). |
| `relay.py` | Adds delivery ids from the outbox to the stream (tick, re-relay sweep, host-ready). |
| `worker.py` | The consumer; dispatches through the GitHub core's `HandlerRegistry` (`EventContext`). |
| `reconciler.py` | The reconciler process: relay tick, sweep, delivery reconciliation (`HookDeliveryClient`), retention, trimming. |
| `lease.py` | Fenced scope leases (`LeaseManager`, `guarded_execute`, `guarded_insert`, `bounded_call`); `LeaseManager` and `Lease.guard()` are the adapter core's `LeaseProvider` and `Lease`. |
| `secrets.py` | `LocalSecrets` (`file:` / `env:` refs to read, `store:` refs under `SECRET_STORE_DIR` to read, write, delete and list) and its in-memory twin; the `SecretReader` / `SecretWriter` of the GitHub core. |
| `ports.py` | The ports of the GitHub and adapter cores over these components: `SqlTenants` / `MemoryTenants` (installation store, installation resolver, tenant resolver), `ParkedValues` (value store, in the secret store), `ConnectionData` (tenant connection store, config resolver), `ReconcileRequests`. |
| `schema.py` | The schema gate (`schema_compat` integer) and its readiness check. |
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
| `STREAM_CLAIM_IDLE_SECONDS` | 120 | A pending entry idle this long is claimed from a dead consumer. Keep it above the longest handler run (the lease TTL). |
| `LEASE_TTL_SECONDS` / `ADAPTER_CALL_TIMEOUT_SECONDS` | 120 / 30 | The timeout must be shorter than the TTL (validated). |
| `BODY_RETENTION_PROCESSED_DAYS` / `_DEAD_DAYS` / `DELIVERY_ID_RETENTION_DAYS` | 7 / 30 / 30 | Bodies are purged after these; the row stays until the id retention elapses. |
| `RELAY_RECHECK_SECONDS` | 900 | The re-relay sweep. |
| `SCHEMA_WAIT_SECONDS` | 300 | How long a process waits for the schema before it exits non-zero. |
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
  counted), any other exception is a counted failure.
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
