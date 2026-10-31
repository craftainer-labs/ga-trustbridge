# FR-0010. Provide a liveness health endpoint

## Status

Implemented

## Description

The system shall expose `GET /health/live` returning
`{"status": "ok"}` unconditionally, without checking any dependency,
for use as a process-liveness probe.

## Source

Operators/SRE, container orchestrators (Kubernetes). Implemented in
`src/app/controllers/health.py`.

## Acceptance criteria

- `GET /health/live` returns 200 as long as the process is running,
  regardless of the state of Postgres, Redis, S3, or the OIDC
  provider.
