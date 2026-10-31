# tests/e2e/

Playwright end-to-end tests — the third of `tests/`'s three suites (see
`../README.md`), run from the devcontainer itself, with Playwright
driving Chromium remotely in the `selenium` stack container over CDP
(see `.devcontainer/stack/selenium/README.md` and this directory's
`conftest.py`) — not collected by a plain `pytest` run (see
`pyproject.toml`'s pytest `addopts`, which ignores this directory by
default; run it explicitly with `uv run pytest tests/e2e`).

The whole suite runs three times per session, parametrized over the
session-scoped `app_leg` fixture (`conftest.py`, `params=["dev", "mock",
"bare"]`; `app_mode` derives `dev`/`mock` from it). The legs differ in which optional
features their `uvicorn` process runs with (`_LEG_ENV` in `conftest.py`), so that every
feature's enabled and disabled code paths execute in some leg and the combined
coverage reaches 100%:

- `dev` (`:8000`, `MODE=dev`): a local connection pool with explicit sizes, TLS
  negotiation (`POSTGRES_SSLMODE=prefer`), asyncpg timeouts, the Redis stats cache,
  `/health/info`, profiling, the GC freeze, and traces + metrics exported over OTLP to
  an in-process receiver the fixture starts (a `http.server` stub, not a service).
- `mock` (`:8001`, `MODE=mock`, `ALLOW_MOCK_MODE=1`): the in-process stats cache,
  `/health/info` with `APP_VERSION`, profiling, no GC freeze, OTel with exporters off,
  and a trusted `X-Forwarded-For` for the rate-limit journey.
- `bare` (`:8002`, `MODE=dev`): none of the optional settings, i.e. the defaults.

`test_crud_sdk_discovery_e2e.py` uses `craftainer_crud_sdk` (a dev dependency; sync and
async clients) to discover the manifest through `/.well-known/api-catalog` and read heroes
with it, and checks the manifest's role filtering and ETag over plain HTTP; it runs on every
leg.

`test_observability_e2e.py` holds the journeys for these features; each adapts to its
leg and skips when the api was started elsewhere (its environment is unknown). Those
journeys: request-ID echo, trace/request IDs in JSON logs, spans and metrics arriving at
the receiver, `/health/info`, `?profile=1`, the stats cache (miss, hit, miss after a
write) and a 429 from the rate limiter. A leg's JSON log lines go to a temp file the
journeys read. The `dev` leg's process migrates on startup, which currently swaps out
its JSON log handler (see `../../src/app/README.md`, "Startup, shutdown and middleware
wiring"), so the log journey runs on `mock`.

The `dev` and `bare` legs use the real services; the `mock` leg uses a second `uvicorn`
process under `MODE=mock`
(`:8001`, `ALLOW_MOCK_MODE=1`) with no Postgres/Redis/S3/Keycloak
dependency at all — `InMemoryRepository`, `MockHealthCheck`, and
`POST /mock/token` (see `../../src/app/README.md`'s "MODE" section)
standing in for each. The mock leg's process still needs the
devcontainer's own Python/uv environment, but none of
`.devcontainer/stack`'s sibling service containers — running just the
mock leg is the one case in this repo where `tests/e2e` doesn't require
the full stack up. `access_token` (`conftest.py`) is mode-aware too: it
reads each dev-realm username's client roles out of
`.devcontainer/stack/keycloak/realm-export.json` and, under `mock`,
mints a token for them via `POST /mock/token` instead of a real
password-grant login, so every test can call `access_token("editor")`
unchanged regardless of mode.

The session-scoped, autouse `_running_app` fixture in `conftest.py` starts
`uvicorn` for each mode's app for the duration of the run and tears it
down afterwards, so this suite doesn't require the app already running
in another terminal — unless something already answers
`{base_url}/health` for that mode (e.g. the "FastAPI: api" launch config
for `dev`), in which case it's left alone, or `E2E_BASE_URL` is set, in
which case the `dev` leg assumes whatever it points at is managed
elsewhere (`E2E_BASE_URL` has no effect on the `mock` leg — its whole
point is not depending on an externally-managed instance).

That `uvicorn` process is a subprocess, so pytest-cov can't measure it
directly the way it measures `../unit/` and `../integration/` importing
`app` in-process. `conftest.py` instead points it at `sitecustomize.py`
(via `PYTHONPATH`) and sets `COVERAGE_PROCESS_START`, so `coverage.py`
starts measuring inside that subprocess on launch and saves on shutdown
(`[tool.coverage.run]`'s `sigterm = true`, in `pyproject.toml`, is what
makes that save actually happen — see the comment there for why);
pytest-cov then combines its data with this process's own at session end
to produce one coverage report against the same 95% floor as `../unit/`
+ `../integration/` (see `../README.md`). A route this suite never
exercises through a real HTTP request stays uncovered here even if
`../unit/`/`../integration/` cover it directly.

Besides the format-regression files at this level (`test_health_e2e.py`,
`test_heroes_e2e.py`/`test_heroes_xml_e2e.py`/`test_heroes_web_e2e.py`,
`test_protected_e2e.py`), one subdirectory per Keycloak client role —
`viewer/`, `editor/`, `maintainer/`, `security/`, `detective/` (see
`.devcontainer/stack/keycloak/realm-export.json`) — holds a single journey
test walking through what that role can and can't do end-to-end, using the
shared `access_token` fixture (`conftest.py`) to log in as it.

## Do

- Run these from the devcontainer's own terminal, same as `../unit/` and
  `../integration/`:

  ```bash
  uv run pytest tests/e2e
  ```

- Use the `base_url` fixture (from `conftest.py`) rather than
  hardcoding `http://myapp:8000`.
- Use the `access_token` fixture (from `conftest.py`) to log in as a
  dev-realm user rather than adding another local
  `_fetch_access_token()`-style helper.
- Reach the real `myapp` service — that's the point of this directory,
  unlike `../unit/`.

## Don't

- Import from `../unit/` or `../integration/`, or vice versa — keep all
  three suites independent so one can run without another's container.
- Add e2e-only dependencies anywhere but the `dev` optional-dependency
  group in `pyproject.toml`.
