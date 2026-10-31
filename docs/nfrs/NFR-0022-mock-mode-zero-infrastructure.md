# NFR-0022. Require zero external infrastructure under MODE=mock

## Attribute

Developer experience.

## Description

Under `MODE=mock`, the app shall boot and be fully functional (CRUD,
health, RBAC) without Postgres, Redis, S3, or Keycloak running.

## Source

Developers. Documented in `src/app/README.md`; implemented across
`src/app/repositories/memory.py`, `src/app/health/checks.py`,
`src/app/controllers/mock.py`, `src/app/oidc.py`.

## Verification

Unit test suite runs entirely under `MODE=mock` (or equivalent fakes)
with no real service dependency, per `tests/README.md`. `tests/e2e` also
runs one full parametrized leg under `MODE=mock` (`tests/e2e/conftest.py`),
driving a real `uvicorn` process with no Postgres/Redis/S3/Keycloak
dependency through real HTTP -- a stronger end-to-end verification than
the unit suite's in-process one.
