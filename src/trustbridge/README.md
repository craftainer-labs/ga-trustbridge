# trustbridge/

The instance package of the trustbridge GitHub App, a sibling of the template's `src/app/`
([ADR 0028](../../docs/adrs/0028-trustbridge-architecture-and-instance-layout.md)). It may import
`app`; `app` never imports it (import-linter). Nothing under `src/app/` is edited.

- `main.py`: the api entrypoint `trustbridge.main:app`. Imports the template app, mounts the
  webhook routes, installs `ProxyHeadersMiddleware`, wraps the template's lifespan (schema gate,
  host startup checks and recheck task, webhook secret cache) and releases its resources after
  the template has shut down.
- `settings.py`: `TrustbridgeSettings` (extends the template's `Settings`). `GITHUB_HOSTS` and
  `SIGNERS` are typed with the GitHub core's `GitHubHostConfig` and the signers' `SignerConfig`
  (one definition each); the settings-only host checks (`validate_hosts`) are fatal. Also
  near-miss detection and the startup logging of effective defaults.
- `webhooks/`: the GitHub webhook ingress, the Argo CD notification route and the abuse limiter.
  See its `README.md`.
- `worker/`: the shared runtime core (outbox, stream, worker, reconciler, leases, schema gate,
  secret store, the ports over these for the GitHub and adapter cores, migrate Job). See its
  `README.md`.
- `github/`: the GitHub core (hosts, availability, tokens, writer, reporters, handler registry,
  `runtime.build_host_runtime`). See its `README.md`.
- `adapters/`: the adapter plugin interface (I2) and the credential core. See its `README.md`.
- `signer/`: the `file` and `vault-transit` App-key signers. See its `README.md`.
- `reconciler/`, `migrations/`: one-line `__main__` shims so the commands keep the names in the
  ADR (`python -m trustbridge.reconciler`, `python -m trustbridge.migrations upgrade`).

Import rules (import-linter, `pyproject.toml`): `main` > `webhooks` > `worker` > `settings`;
`app` never imports `trustbridge`; `adapters` and `signer` never import `github`.

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

Not wired yet: the admin manifest-flow endpoints (`github/manifest.py`) are NG6's and stay
unmounted; the `CredentialCore` is not instantiated by any process because no adapter exists
(NG5): the lease, value store and connection-data ports it needs are built in `Core` for it; the
reconcile requests (`ReconcileScheduler`) are recorded in the Valkey set `tb:reconcile`, whose
consumer is the reconcile loop of NG5/NG6.

## Don't

- Edit template-owned files to hook in (the `Dockerfile`, `scripts/runner.sh`, `src/app/`);
  deployments override the entrypoint instead.
- Run migrations from the api, worker or reconciler.
