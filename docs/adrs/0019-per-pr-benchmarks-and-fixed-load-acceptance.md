# 0019. Add per-PR microbenchmarks, a relative load check and fixed-load acceptance

## Status

Accepted

## Context

[`0010`](0010-locust-for-load-testing.md) chose Locust and ran the
load test only on `workflow_dispatch` plus a weekly schedule, because
wall-clock load tests on shared runners are too noisy to gate PRs. That
left two gaps: a regression could ride on `main` for up to a week, and
nothing answered "how much load can the `runner` image serve", only "did
the `NFR-0024` ceilings still hold at 20 users".

The performance work in the packages/performance plan needs every change
accepted or rejected on numbers. That requires a reproducible hardware
budget, a capacity metric, a low-noise per-PR signal, and a rule for
deciding whether a measured difference is real. This ADR amends
`0010` by superseding its "manual or weekly only, no per-PR performance
signal" part. Locust, the `runner`-image target and the real backing
services stay as `0010` decided.

Measured facts that shape the rules (image `perf-baseline:07154b9`,
1 vCPU / 2 GiB, 2026-10-18):

- Capacity, 5-user steps, 150 s per step, 30 s warm-up: `max_users_passed`
  25 / 30 / 25 (median 25, spread 5). Steps fail on tail latency (list
  `GET`, `PATCH`), not CPU saturation: api CPU mean 44-52% of the limit,
  RSS about 157 MiB, 24-28 RPS, p95 140-160 ms, p99 200-240 ms, 0 errors.
- A 3% capacity-improvement rule cannot resolve anything at 5-user
  steps (one step is 20% of the median).
- Fixed load, 25 users, 6 min, first 60 s excluded: CPU per request
  18.95 / 19.69 / 19.60 ms (median 19.60, spread 3.8%); aggregate p99
  210 / 210 / 270 ms (spread 28.6%). These CPU figures came from
  `docker stats` sampling, which understated CPU about 2.6x on an idle
  container, so they are not comparable with later exact cgroup
  numbers. Exact-method baseline (cgroup `cpu.stat`, same image,
  median of three runs): 22.08 CPU ms/request (spread 14.2%), aggregate
  p99 280 ms, time to ready 1.59 s. The exact-method spread is wider
  than the `docker stats` one, so the rule below needs a larger effect
  than the 3.8% first estimated. Capacity was re-measured at 35 users
  (earlier runs 25 / 30 / 25; 5-user steps).
- Whole-host stalls of 9-15 minutes hit about one run in five on the
  8 GB development host.

## Decision

We will measure at a fixed reference budget and accept changes on fixed
load, with CI layers that match each signal's noise level.

**Reference budget.** All capacity and fixed-load numbers refer to the
`runner` image capped at 1 vCPU and 2 GB (`compose.perf.yml`:
`cpus: "1"`, `mem_limit: 2g`), the same budget as the "$12 server"
challenge that motivated this work. Backing services stay uncapped so
the app is the bottleneck. `compose.perf.yml` also sets
`RATE_LIMIT_BULK_ACTION=1000000/minute` (all Locust users share one IP)
so only the threshold changes while the limiter's Redis round trip stays
measured.

**Acceptance rule (supersedes the earlier "median capacity beats the
baseline by more than the spread and by 3%" rule).** Each variant gets
three fixed-load runs at 25 users, just below the baseline break point.
Keep a change only if api CPU per request, or aggregate p99, improves by
more than the baseline run-to-run spread and by at least 3%, without the
other regressing beyond its spread. The spread is the exact-method one
(14.2% for CPU per request at the baseline); an effect inside it is
noise. Capacity is a coarse gate only: a
kept change must not lower the median `max_users_passed` by a step. With
the current spreads, CPU per request is the deciding metric and p99 is a
regression guard. Runs with a sampler gap over 10 s or worker heartbeat
errors (host stalls) are discarded and repeated. Expected effects of
2-5% are measured interleaved (A/B/A/B) to cancel drift.

**Per-run record.** Each run stores p50/p95/p99, RPS, error rate and
exact container CPU (cgroup `cpu.stat` usage delta) and RSS in one JSON
summary beside Locust's CSV/HTML, plus generator CPU so the load
generator is shown not to be the bottleneck.

**CI layers** (extending `0010`'s split; noisy wall-clock load tests
still do not gate every PR):

| Layer | Trigger | Gate |
| --- | --- | --- |
| Microbenchmarks in `tests/bench/` (a fifth suite, not collected by the default `pytest` run so it cannot distort the coverage gate) | every PR and push to `main`, via `codspeed.yml` | [CodSpeed](https://codspeed.io/) simulation mode counts instructions, not wall-clock time; its PR regression report fails above a set percentage |
| Fixed-load regression (`perf.yml`, `fixed` job) | weekly, `workflow_dispatch`, PRs labelled `perf` | `NFR-0024` absolute ceilings, plus a relative p95 check against the latest `main` run's `perf-summary` artifact (default `PERF_MAX_P95_REGRESSION_PERCENT` 20; passes when no baseline exists) |
| Capacity search (`perf.yml`, `capacity` job) | `workflow_dispatch` only | none (report only): max users, CPU, RSS and a py-spy flame graph as artifacts |

**CodSpeed** was chosen for a long open-source track record (running
since 2022; used by Pydantic and Astral's ruff and uv), a free plan for
public repositories, and MIT-licensed client tooling
(`CodSpeedHQ/action`, `pytest-codspeed`). Lock-in is low because
benchmarks remain plain `pytest-benchmark` tests. Fallbacks, in order:
Bencher, then `pytest-benchmark --benchmark-compare` with
`github-action-benchmark`. After the package extraction
([`0020`](0020-extract-crud-and-observability-into-published-packages.md)),
each package benchmarks its own hot paths in its own repo; this repo's
`tests/bench/` keeps the app-level cases (token verification, the ASGI
round trip).

`perf.yml` keeps honouring `vars.CI_RUNNER`; a dedicated self-hosted
runner would make the relative check more trustworthy than shared
runners.

## Consequences

Easier: a regression in instruction counts is visible on the PR that
caused it; a performance change is accepted or rejected by a rule fixed
before measuring; capacity and CPU per request are reported with every
result, so "same capacity at half the CPU" counts as an improvement.

Harder: the fixed-load rule can only detect CPU-per-request changes
above about 3.8% (the current spread), so smaller wins are unprovable and
are rejected by design. Measurements need an otherwise idle host and
repeats, which costs time. CodSpeed adds an external service and an
org-level app installation; simulation mode counts instructions, so it
cannot see I/O or event-loop effects, which the Locust layers still own.
The relative check is only as trustworthy as the runner it runs on.
With the exact-method baseline the CPU spread is 14.2%, not 3.8%, so
only changes above that are provable. The CodSpeed failure threshold was
not measured or tuned in this work; it is set in CodSpeed's own
settings once a few `main` runs show the noise of the instruction
counts.

## Results

Fixed load, 25 users, 300 s window, exact cgroup CPU, three runs per
variant, medians (reference budget 1 vCPU / 2 GiB):

| Variant | CPU ms/request | Aggregate p99 | Capacity (`max_users_passed`) |
| --- | --- | --- | --- |
| Baseline `07154b9` | 22.08 (spread 14.2%) | 280 ms | 35 users (5-user steps) |
| HEAD, `DB_POOL_MODE=null` | 19.11 (-13.4%, within the baseline spread) | 200 ms | not re-measured |
| HEAD, `DB_POOL_MODE=local` | 7.31 (-61.7% vs `null`); p50 11 ms, p95 62 ms | 68 ms | not re-measured |
| Final, packages 0.1.1 from PyPI (`4a44d9c`), `local`, defaults | 7.244 (`6794d38`: 7.236, no change) | 69 ms | 130 users (10-user steps; `6794d38`: 120) |

Overall, the final state against the baseline: -67% CPU per request,
p99 280 to 69 ms, capacity about 3.7x. The pool mode
([`0022`](0022-db-pool-mode-per-deployment.md)) is the one change that
cleared the rule by a wide margin.

Candidates that did not clear the rule (CPU per request beyond the
spread and at least 3%):

| Knob | Effect | Outcome |
| --- | --- | --- |
| Access log off | -2.1% (`6794d38`) | below 3% and inside the spread |
| `GC_FREEZE=false` | +0.6% | no effect |
| Granian instead of uvicorn | +0.8% | uvicorn stays; no ADR for a server switch |

OpenTelemetry overhead is in [`0021`](0021-opentelemetry-traces-and-metrics-pushed-over-otlp.md);
the shutdown check under load is in the plan's phase 7.6 record
(2256 of 2256 requests accepted before `SIGTERM` finished without a 5xx,
exit code 0 within 0.19 s).

### Kept for correctness

The rule decides performance changes, not correctness ones. On
2026-10-19 the user decided to keep the changes F1-F3, R2 and
`gc.freeze()` even though their measured CPU effect was within noise,
because their value is correctness and robustness (no regression was
measured). A later change of this kind needs the same explicit
decision; a performance-motivated change without a measured win beyond
the spread is rejected.

### Considered and not done

Each of these was rejected before or during measurement, and needs a new
decision (and a measured win under this ADR's rule) to revisit:

- **Caching verified tokens.** RS256 verification costs about 48 µs per
  call (the `tests/bench` token benchmark measures ~79 µs including PyJWT's
  claim checks), small next to a Postgres round trip. A verified-claims
  cache would trade that for stale trust after key rotation or a role
  change, plus cache-key pitfalls (different encodings of one token). Revisit
  only if a flame graph shows verification above about 10% of CPU. A
  hand-rolled verifier is out for the same security reason.
- **Python's JIT and the free-threaded build.** The JIT is experimental
  (-10% to +20% depending on workload); the free-threaded build disables
  it, risks asyncpg/uvloop compatibility and gains nothing at 1 vCPU.
- **gzip in the app.** It spends the single core's CPU; the ingress
  should compress.
- **Granian instead of uvicorn.** Measured: +0.8% CPU per request, no
  capacity gain, more RSS. uvicorn stays the default; `APP_SERVER=granian`
  remains available.
- **Bypassing framework layers** (raw responses instead of typed views,
  dropping dependency injection, building JSON in the database, batching
  writes across requests, weaker durability): each either reimplements
  FastAPI/SQLAlchemy behaviour or trades away correctness, so they're out
  of scope regardless of numbers.
