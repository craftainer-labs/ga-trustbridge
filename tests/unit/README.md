# tests/unit/

Pure unit tests: no real external service, no network.

## Do

- Reach for a fixture or mock when a route/function needs one of the
  stack services — that's what makes it a unit test.

## Don't

- Reach a real external service (Postgres, Redis, S3, Keycloak) — that
  belongs in `../integration/`.
