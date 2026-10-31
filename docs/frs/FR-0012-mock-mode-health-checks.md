# FR-0012. Replace health checks with always-healthy stubs under MODE=mock

## Status

Implemented

## Description

The system shall, under `MODE=mock`, replace every real dependency
health check with an always-healthy stub reporting `detail: "mocked"`,
so readiness succeeds without any real infrastructure running.

## Source

Developers, CI. Implemented in `src/app/health/checks.py`,
`src/app/health/registry.py`.

## Acceptance criteria

- Under `MODE=mock` with no Postgres/Redis/S3/OIDC provider running,
  `GET /health/ready` still returns 200.
