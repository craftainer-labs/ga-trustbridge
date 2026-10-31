# 0010. Use Locust for load testing, against the runner image, manually triggered

## Status

Accepted. Its "manual or weekly only, no per-PR performance signal" part is
superseded by [0019](0019-per-pr-benchmarks-and-fixed-load-acceptance.md);
the rest stands.

## Context

`tests/` has unit, integration, and e2e coverage (`tests/README.md`),
but nothing exercises the app under concurrent load. `NFR-0016`
documents the async engine's deliberate `NullPool` trade-off (no
in-process connection reuse) but says its verification is "code review
against the documented rationale ... no automated performance check
exists today" — a regression in that trade-off, or any other
latency/throughput regression, has no automated signal.

A load-generation tool needs to: script weighted, per-endpoint request
mixes against a real running app (not `TestClient`, which never opens a
socket or a real connection pool); run headless in CI with a
machine-checkable pass/fail outcome; and fit this repo's single-language,
`uv`-managed toolchain (`pyproject.toml`'s "Versions and config") rather
than adding a second runtime the way k6 (Go/JS) or Gatling (JVM) would.

## Decision

We will use [Locust](https://locust.io/) as the load-generation tool,
added to the `dev` optional-dependency group in `pyproject.toml` (`uv
add --optional dev locust`) — pure Python, so it needs no new language
runtime or CI toolchain step, and its per-`User`/`@task` scripting style
fits scripting the existing worked example
(`/crud/v1/heroes/v2/json`) the same way `tests/e2e/`'s Playwright
tests do.

The suite lives at `tests/perf/`, a fourth, independent suite alongside
`unit/`/`integration/`/`e2e/` — not collected by `pytest` at all, since
Locust has its own headless runner
(`locust --headless --csv=... --exit-code-on-error`) and a different
execution model (concurrent simulated users, not one assertion per
test). It targets the `runner`-stage image over the real backing-service
stack (`compose.yml`, the same one `smoke.yml` builds), not the
devcontainer's `dev` `uvicorn --reload`/debugpy-attached process —
profiling a reload-enabled, debugger-attached process would misrepresent
production latency, and `MODE=mock`'s in-memory fakes would misrepresent
real Postgres/Redis/S3/Keycloak round trips, defeating the point of a
suite meant to catch a regression like `NFR-0016`'s pooling trade-off.

The new `.github/workflows/perf.yml` job runs on `workflow_dispatch`
(plus a weekly `schedule`) only — unlike `checks.yml`/`smoke.yml`, it
does not run on every push/PR. A load test's runtime (bringing up the
full stack, then sustaining load for long enough to get a stable p95) and
its sensitivity to shared-runner noise (a noisy-neighbor CI runner
produces a false latency regression, unlike a deterministic
pass/fail unit test) make it a poor fit for a per-PR gate; a human
reviewing the reason before rerunning it is more useful than blocking
merges on a flaky signal.

## Consequences

Easier: a real, repeatable signal exists for `NFR-0024`'s latency/
throughput targets and for `NFR-0016`'s pooling trade-off, runnable
on demand or on a schedule, with its CSV/HTML report retained as a
CI artifact for after-the-fact inspection. Staying Python-only means no
second toolchain to install, pin, or teach `mypy`/`ruff` about.

Harder: the new CI job costs runner time (building the `runner` image
plus the whole backing-service stack, every run) that `checks.yml`
doesn't pay, and being manual/weekly rather than per-PR means a
regression can land and ride on `main` for up to a week before the
scheduled run catches it. Load results also only cover what's actually
scripted in `tests/perf/locustfile.py` — an endpoint or code path with
no task never gets a load signal, so this suite narrows, but doesn't
replace, `NFR-0016`'s existing code-review gate for unscripted paths.
