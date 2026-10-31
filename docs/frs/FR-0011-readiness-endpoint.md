# FR-0011. Provide a readiness health endpoint aggregating dependency checks

## Status

Implemented

## Description

The system shall expose `GET /health/ready`, running every registered
health check concurrently (Postgres, Redis, S3, OIDC provider),
returning 200 with a per-check breakdown when all are healthy, and 503
with `status: degraded` and the same breakdown otherwise.

## Source

Operators/SRE, container orchestrators. Implemented in
`src/app/health/checks.py`, `src/app/health/registry.py`,
`src/app/controllers/health.py`.

## Acceptance criteria

- With all dependencies healthy, `GET /health/ready` returns 200 and
  `status: ok`.
- With any one dependency unhealthy, it returns 503, `status:
  degraded`, and identifies which check(s) failed and why.
