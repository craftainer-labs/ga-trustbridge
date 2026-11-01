# trustbridge/

The instance package of the trustbridge GitHub App, a sibling of the template's `src/app/`
([ADR 0028](../../docs/adrs/0028-trustbridge-architecture-and-instance-layout.md)). It may import
`app`; `app` never imports it (import-linter). Nothing under `src/app/` is edited.

- `main.py`: the api entrypoint `trustbridge.main:app`. Imports the template app, mounts the
  webhook router, installs `ProxyHeadersMiddleware` and wraps the template's lifespan.
- `settings.py`: `TrustbridgeSettings` (extends the template's `Settings`), the host and signer
  models, near-miss detection and the startup logging of effective defaults.
- `webhooks/`: the ingress route and the abuse limiter. See its `README.md`.
- `worker/`: the shared runtime core (outbox, stream, worker, reconciler, leases, hosts, schema
  gate, migrate Job). See its `README.md`.
- `reconciler/`, `migrations/`: one-line `__main__` shims so the commands keep the names in the
  ADR (`python -m trustbridge.reconciler`, `python -m trustbridge.migrations upgrade`).

Import order: `main` > `webhooks` > `worker` > `settings`.

## Don't

- Edit template-owned files to hook in (the `Dockerfile`, `scripts/runner.sh`, `src/app/`);
  deployments override the entrypoint instead.
- Run migrations from the api, worker or reconciler.
