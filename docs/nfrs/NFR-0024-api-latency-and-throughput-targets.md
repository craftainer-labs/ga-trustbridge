# NFR-0024. API responses stay within measured latency/error-rate ceilings under load

## Status

Accepted

## Attribute

Performance.

## Description

Under sustained load against the `runner` image over the real backing-
service stack (`compose.yml`, `MODE=production` — not `MODE=mock`), the
Hero CRUD resource and the liveness probe shall stay within:

- **CRUD read/write endpoints** (`/crud/v1/heroes/v2/json`,
  list/create/get/update/delete): **p95 latency under 200ms**, **0%
  error rate**, at 20 concurrent simulated users.
- **`/health/live`**: **p95 latency under 50ms**, **0% error rate**, at
  the same load.

These targets were set from a measured baseline, not guessed: a 2-minute
`tests/perf/locustfile.py` run (20 users, spawn rate 5, `maintainer`
role) against a freshly built `runner` image and its real Postgres/
Redis/S3/Keycloak stack produced 2,326 requests with a 0% error rate and
these p95s: `GET /crud/v1/heroes/v2/json` 89ms, `POST` 90ms, `GET ?id=`
80ms, `PATCH ?id=` 86ms, `DELETE ?id=` 88ms, `/health/live` 14ms
(aggregate p95 85ms, ~19.5 req/s). The ceilings above sit at roughly 2x
that measured p95, leaving headroom for normal variance while still
catching a real regression (e.g. a pooling change under `NFR-0016`, or
an N+1 introduced in the repository layer) rather than chasing exact
baseline noise. Revisit these numbers (as a new, superseding NFR) once
more runs establish how much they vary run to run.

## Source

Developers; performance/operations, prompted by `NFR-0016`'s
`NullPool` trade-off having no automated verification. See
`docs/adrs/0010-locust-for-load-testing.md` for the tool/approach
decision.

## Verification

`tests/perf/locustfile.py`, run headless with `--exit-code-on-error`
(see `tests/perf/README.md`), via `.github/workflows/perf.yml` on
`workflow_dispatch` and a weekly schedule. Not run per-PR — see the ADR
above for why.
