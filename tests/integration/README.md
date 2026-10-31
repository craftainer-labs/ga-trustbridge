# tests/integration/

Tests that reach a real stack service — Postgres, Redis, S3,
Keycloak — already running under the devcontainer (and under CI via
`devcontainers/ci`), instead of mocking it. `test_oidc.py` is the
current example: it fetches a real token from the live `keycloak`
container and validates it through `app.oidc.decode_bearer_token`.

## Do

- Read connection details from `app.config.get_settings()`, the same
  settings the app itself uses, rather than hardcoding a service's
  hostname/port here.
- Add a test here — following the same real-service, no-mocks pattern —
  for any new code that talks to Postgres/Redis/S3.

## Don't

- Mock the service under test — that's what makes this suite different
  from `../unit/`.
- Assume these run outside the devcontainer/CI — they need the
  stack containers actually up.
- Add readiness-polling for a stack service here — each service's own
  `healthcheck:` plus `myapp`'s `depends_on: condition: service_healthy`
  (see `../../.devcontainer/stack/README.md`'s "Devcontainer stack
  pattern" section) already
  guarantees it's ready before `myapp`, and therefore this suite, starts.
