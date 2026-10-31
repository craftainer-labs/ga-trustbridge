# tests/perf/

Locust load test -- the fourth, independent suite alongside `../unit/`,
`../integration/`, and `../e2e/` (see `../README.md`). Not `pytest`-based
at all: Locust has its own headless runner, so this directory is never
collected by a plain `pytest` run (nor by `uv run pytest tests/e2e`) and
is never included in `--cov` -- see `docs/adrs/0010-locust-for-load-
testing.md` for why.

Unlike `../e2e/`, which drives the devcontainer's own `dev`/`mock`
processes, this suite targets the `runner`-stage image (`compose.yml` at
the repo root, `MODE=production`, the same image `../../.github/workflows/
smoke.yml` and `release.yml` ship) over its real Postgres/Redis/S3/
Keycloak backing services -- profiling a reload-enabled, debugger-attached
`dev` process, or `MODE=mock`'s in-memory fakes, would misrepresent
production latency and skip the real database round trips this suite
exists to catch a regression in (see `docs/nfrs/NFR-0016-no-connection-
pooling-tradeoff.md` and `docs/nfrs/NFR-0024-api-latency-and-throughput-
targets.md`, the requirement this suite verifies).

`locustfile.py` scripts the worked example
(`/crud/v1/heroes/v2/json` list/create/get/update/delete, weighted toward
reads) plus `/health/live`, logging in against the real dev-realm
Keycloak (password grant, same convention as `../e2e/conftest.py`'s
`access_token` fixture) since `POST /mock/token` is only mounted under
`MODE=mock`, which this suite deliberately doesn't run under.

## Do

- Run it from the devcontainer's own terminal, against the `runner`
  image's stack:

  ```bash
  docker compose -f compose.yml up -d --build --wait
  uv run locust -f tests/perf/locustfile.py --headless \
      --host http://localhost:8000 \
      --users 50 --spawn-rate 5 --run-time 2m \
      --csv tests/perf/.results/run --html tests/perf/.results/run.html \
      --exit-code-on-error 1
  docker compose -f compose.yml down --volumes --remove-orphans
  ```

  `--exit-code-on-error` makes Locust exit non-zero if any request errors;
  `locustfile.py`'s own `quitting` event listener additionally fails the
  run if any endpoint's p95 latency exceeds `NFR-0024`'s ceiling
  (Locust has no built-in latency-threshold flag).
- Pass `--revalidate-weight N` (default 0, off) to add a conditional-GET
  task: `GET ?id=` with the `If-None-Match` from the user's previous
  response, expecting `304`. The baseline request mix is unchanged
  unless it's set.
- Authenticate against the stack's own Keycloak when running from the
  devcontainer. `compose.yml`'s api only accepts tokens whose issuer is
  `https://keycloak:8443/realms/template-fastapi`, but inside the devcontainer
  the name `keycloak` resolves to the devcontainer's own Keycloak (and
  `get_settings()` points at it), so without the steps below most requests
  get 401. The stack publishes its Keycloak on host port 8443 and Keycloak
  echoes the request's `Host` header into the token's `iss`, so
  `locustfile.py` can connect to `127.0.0.1:8443` while sending
  `Host: keycloak:8443` and TLS server name `keycloak`. Set, in the shell that
  runs Locust (no `.devcontainer/` or app change needed):

  ```bash
  docker compose -f compose.yml -f compose.perf.yml cp smoke-tls-cert:/tls/keycloak.crt tests/perf/.results/keycloak.crt
  export OIDC_TOKEN_URL=https://keycloak:8443/realms/template-fastapi/protocol/openid-connect/token
  export PERF_OIDC_CONNECT_URL=https://127.0.0.1:8443   # where token requests actually connect
  export SSL_CERT_FILE=$PWD/tests/perf/.results/keycloak.crt
  ```

  `PERF_OIDC_CONNECT_URL` is optional and unset in CI, where
  `.github/workflows/perf.yml` instead maps `keycloak` to 127.0.0.1 in
  `/etc/hosts`.
- Reuse `app.config.get_settings()` for the OIDC token/client-id values
  (as `locustfile.py` already does) rather than hardcoding Keycloak's
  URL a second time.
- Check the load generator isn't the bottleneck: add `--processes -1`
  (one Locust worker per core, fork-based, so no Windows) to any command
  here and read the `generator_cpu_percent` block of the summary (below).
  Locust's own "CPU usage above 90%" warning sets `cpu_warning: true`
  there too; a run with that flag set measures the generator, not the
  api, so rerun it with more workers or a bigger box.

### Authentication during long runs

The realm's access tokens last 300s, so `locustfile.py` does not log in once
per user. One expiry-aware token is shared by all users of a Locust process
(same credentials; one Keycloak password grant per worker instead of one per
user) and renewed once 80% of its `expires_in` has passed. As a safety net, a
401 makes the user fetch a fresh token and retry that request once. Token
requests go through httpx, not Locust's client, so they never show up in the
stats; the 401 itself is recorded as a failure, so a run that needs the safety
net (or whose login is broken, which raises instead of retrying in a loop) is
visible. Each process logs `Token logins in this ... process: {...}` (counts
of `login`, `refresh`, `relogin`) at quit.

### Capacity mode

The default run above is the fixed-load `NFR-0024` check ("did we
regress?"). Capacity mode answers "how far can we go?": it steps users up
and reports the highest step that still met the ceilings. It is opt-in
through one switch, `--perf-mode capacity` (env `LOCUST_PERF_MODE`); the
default `--perf-mode fixed` is exactly the run above. In capacity mode
`--users`/`--spawn-rate`/`--run-time` are ignored (the shape decides), and
the fixed-mode p95 check is skipped, because later steps are expected to
breach it. The shape class is `abstract` so Locust doesn't auto-pick it;
the `init` listener installs it only for this mode.

```bash
uv run locust -f tests/perf/locustfile.py --headless --processes -1 \
    --host http://localhost:8000 --perf-mode capacity \
    --capacity-step-users 10 --capacity-step-duration 120 \
    --capacity-warmup 30 --capacity-max-users 200 \
    --csv tests/perf/.results/cap --html tests/perf/.results/cap.html \
    --summary-json tests/perf/.results/cap.json
```

Do not add `--exit-code-on-error` here: the failing step produces
errors by design. The run exits non-zero only if not even the first step
passed (`max_users_passed` is 0).

| Argument | Default | Meaning |
|---|---|---|
| `--capacity-step-users` | 10 | Users added per step |
| `--capacity-step-duration` | 120 | Seconds per step |
| `--capacity-warmup` | 30 | Seconds at the start of each step discarded from that step's numbers; must cover the spawn ramp plus each new user's Keycloak login |
| `--capacity-max-users` | 200 | Last step's users (steps = ceil(max / step users)) |
| `--capacity-spawn-rate` | 10 | Users spawned per second |
| `--capacity-p99-factor` | 2.0 | p99 ceiling = factor x the endpoint's p95 ceiling (`NFR-0024` gives no p99) |
| `--capacity-min-endpoint-samples` | 100 | Per-endpoint p95/p99 ceilings apply only to endpoints with at least this many requests in the step's window |
| `--capacity-max-error-rate` | 0.0 | Highest error rate (0-1) in a passing step |
| `--capacity-run-all-steps` | off | Keep going after the first failing step (default stops there) |

A step passes when, over its post-warm-up window:

- the aggregate p95 is within the default endpoint ceiling (200ms,
  `_DEFAULT_P95_LATENCY_MS_CEILING`), the aggregate p99 within that times
  `--capacity-p99-factor`, and the aggregate error rate within
  `--capacity-max-error-rate` (always checked);
- every endpoint's error rate is within `--capacity-max-error-rate`
  (always checked, however few requests it had);
- every endpoint with at least `--capacity-min-endpoint-samples` requests
  in the window has its p95 within `NFR-0024`'s ceiling (200ms; 50ms for
  `/health/live`) and its p99 within the derived ceiling. Rarer endpoints
  (a p95 over a handful of requests is noise) are skipped for latency.

The window starts at the first shape tick (about 1 Hz) after the warm-up,
so it is accurate to about a second. Each step in the summary says which
endpoints had their latency checked (`checked`) and why a failing step
failed (`failed_on`). `max_users_passed` is the highest passing step's users (0 if none).

Tiny smoke run (two 40-second steps, 10-second warm-up):

```bash
uv run locust -f tests/perf/locustfile.py --headless --host http://localhost:8000 \
    --perf-mode capacity --capacity-step-users 5 --capacity-max-users 10 \
    --capacity-step-duration 40 --capacity-warmup 10 \
    --summary-json tests/perf/.results/smoke.json
```

Note that `locust --help` doesn't list these arguments (Locust 2.46
builds the help text before it imports the locustfile); the table above
is the reference.

### JSON summary

`--summary-json <path>` (default: none) writes one JSON file per run,
in both modes, when the run quits (not written by distributed workers):

```json
{
  "mode": "fixed | capacity",
  "aggregate": {"requests": 0, "p50": 0, "p95": 0, "p99": 0, "rps": 0.0, "error_rate": 0.0},
  "endpoints": {"GET /health/live": {"same keys as aggregate": 0}},
  "generator_cpu_percent": {"max": 0.0, "mean": 0.0, "samples": 0, "workers": 0, "cpu_warning": false},
  "steps": [{"users": 10, "p50": 0, "p95": 0, "p99": 0, "rps": 0.0,
             "error_rate": 0.0, "passed": true, "failed_on": [],
             "endpoints": {"POST /...": {"same keys as aggregate": 0, "checked": true}}}],
  "max_users_passed": 10,
  "api_resources": {}
}
```

- Latencies are in ms, `error_rate` is 0-1. `aggregate`/`endpoints` are
  cumulative over the whole run (including ramp and warm-up); in capacity
  mode use `steps`, which only count each step's post-warm-up window.
- `steps` and `max_users_passed` appear in capacity mode only. Each
  step's top-level numbers are the aggregate over all endpoints;
  `endpoints` breaks them out per endpoint, with `checked` (bool: whether
  the endpoint had enough samples for its latency ceilings to apply).
  `failed_on` lists the breached ceilings, e.g. `"Aggregated p95 250ms >
  200ms"` (empty when the step passed).
- `generator_cpu_percent`: percent of one core, the busiest generator
  process (master or worker) per 5-second sample; `workers` is the peak
  number of workers connected during the run (0 without `--processes`). `null` values mean the
  run was shorter than one sample.
- `api_resources` is present only if `--stats-json <path>` names a file
  that exists when the run quits. It embeds the `api` container sampler's
  output as-is (`container`, `interval_s`, `samples`, and `summary` with
  `cpu_percent` mean/p95/max and `mem_bytes` mean/max). `stats.py` only writes
  its file when it stops, so stop it (SIGINT, or `--duration` shorter than the
  run) before Locust quits; in capacity mode Locust quits right after the first
  failing step unless `--capacity-run-all-steps` is set.

## Don't

- Import from `../unit/`, `../integration/`, or `../e2e/`, or vice versa
  -- same independence rule as the other three suites (`../README.md`).
- Run this against the devcontainer's own `dev` `uvicorn --reload`
  process, or with `MODE=mock` -- see above for why that would
  misrepresent the numbers this suite measures.
- Add this directory to `pyproject.toml`'s `addopts`/coverage config --
  it isn't `pytest`-collected and never will be.

## Resource sampling and profiling

- `../../compose.perf.yml` is a perf-only override that caps `api` at
  `cpus: "1"` / `mem_limit: 2g` (the reference budget capacity numbers refer
  to) and grants it `SYS_PTRACE`; backing services stay uncapped. Layer it
  with `docker compose -f compose.yml -f compose.perf.yml up -d --build --wait`.
- `stats.py` samples a container's CPU and memory while a run is in
  progress, then writes a JSON file with the samples and a summary
  (mean/p95/max CPU, mean/max memory, `mem_peak_bytes`) when stopped:

  ```bash
  uv run python tests/perf/stats.py --container <api-container> \
      --interval 2 --out tests/perf/.results/stats.json &   # SIGINT/SIGTERM or --duration ends it
  ```
- It makes no docker calls while sampling: one `docker inspect` at start
  resolves the full container id, then it reads the container's cgroup v2
  files directly (`/sys/fs/cgroup/docker/<id>/`, or the systemd layout
  `system.slice/docker-<id>.scope/`): `cpu.stat` and `memory.current` per
  sample, `memory.peak` once at the end. It exits 1 at start if the cgroup
  directory isn't visible. Do not go back to `docker exec`/`docker stats`
  here: calling them every interval froze small (8 GB) hosts during load runs.
- Each sample carries `cpu_usage_usec`, the cgroup's cumulative `usage_usec`
  taken at `cpu_t`; `cpu_percent` is its delta over wall time (100 = one CPU)
  and `mem_bytes` is `memory.current`. `summary` has `cpu_seconds_total` (last
  minus first `cpu_usage_usec`) and `window_s`. This is exact, unlike the mean
  of `cpu_percent` samples, so use it for the fixed-load metric:
  CPU-ms/request = cpu_seconds in window x 1000 / requests in window. To
  restrict to the load window, pass `--since <unix-ts> --until <unix-ts>`, or
  call `cpu_seconds_between(samples, since, until)` from `stats.py` on the
  saved samples (returns `(cpu_seconds, window_s)`).
- `profile.sh [-d SECONDS] [-c CONTAINER]` records a py-spy speedscope
  profile of the server (PID 1 in the container) into `.results/`. The runner
  image has no py-spy and must not gain one, so the script copies the dev
  venv's `py-spy` binary into the container with `docker cp` and runs it
  there as root; `SYS_PTRACE` from `compose.perf.yml` permits the attach.
  It uses `--nonblocking` so sampling doesn't pause the app under load. Run it
  during a capacity run's hold phase, and open the file in speedscope.
- `compare_summary.py --baseline A.json --current B.json
  [--max-regression-percent 20]` compares two `--summary-json` files' aggregate
  p95 and exits 1 if the current one is worse by more than the limit (0 with no
  usable baseline); `.github/workflows/perf.yml` uses it against the latest
  `main` run's summary.
