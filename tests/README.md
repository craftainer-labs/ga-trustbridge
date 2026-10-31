# tests/

Five independent suites:

- `unit/` — no external services; mirrors `src/app/`'s structure.
- `integration/` — reaches the real stack containers (already
  running under the devcontainer, and under CI via `devcontainers/ci`) —
  no mocks.
- `e2e/` — Playwright tests against the live `myapp` service, driving a
  browser in the `selenium` stack container remotely; see its own
  `README.md`.
- `perf/` — a Locust load test against the `runner` image over the real
  stack; not `pytest`-based at all (Locust has its own headless runner)
  — see its own `README.md`.
- `bench/` — `pytest-benchmark` microbenchmarks (token decoding, in-process
  ASGI round trips under `MODE=mock`); no external services, run
  explicitly with `uv run pytest tests/bench --no-cov` — see its own
  `README.md`.

A plain `pytest` run collects `unit/` and `integration/` (both work
inside the devcontainer / CI as-is) and ignores `e2e/` and `bench/`
(see `pyproject.toml`'s `addopts`); `perf/` is never collected by
`pytest` at all, under any invocation.

Test code is type-checked under the same `mypy --strict` settings as
`src/` — the hook runs `uv run mypy src tests` — so tests carry full
annotations too (ruff's `ANN` rules enforce the same).

Both that run and `uv run pytest tests/e2e` independently enforce 100%
coverage of `src/app` (see `[tool.coverage.report]` in `pyproject.toml`)
— e2e's coverage comes from the live `myapp` subprocess, not from the test
process itself; see `e2e/README.md`. `perf/` is exempt from that gate
entirely, the same way `e2e/` would be if it weren't itself measured:
Locust isn't `pytest`/`coverage.py`-instrumented, and it only exercises
what's deliberately scripted, so it was never a candidate for the 100%
floor in the first place; see `perf/README.md`.

An `async def test_...` runs with no `@pytest.mark.asyncio` marker
needed (`asyncio_mode = "auto"` in `[tool.pytest.ini_options]`), and
every async test in one run shares a single event loop
(`asyncio_default_fixture_loop_scope = "session"`) — required because
`src/app/models/base.py`'s async engine/session factory are process-wide
singletons (matching `get_settings()`'s pattern): asyncpg ties a
connection to the event loop that opened it, and a run mixes
pytest-asyncio's loop with `TestClient`'s own background-thread loop, so
pooling a connection across that boundary raises
`asyncpg.exceptions.InterfaceError`. The engine also uses `NullPool` (a
fresh connection per checkout) for the same reason, at the cost of
connection reuse.

## Do

- Name a test file after the module or integration point it covers,
  mirroring `src/app/`'s directory structure (`src/app/config.py` →
  `tests/unit/test_config.py`; `src/app/controllers/heroes.py` →
  `tests/unit/controllers/test_heroes.py`, with `__init__.py` in each
  new test subdirectory) so coverage is easy to eyeball.
- Let `assert` and bare literal comparisons stand — `S101` and `PLR2004`
  are disabled for all three suites precisely so tests can look like
  tests.
- Monkeypatch the real object, imported from where it's defined
  (`from redis.asyncio import Redis` → `monkeypatch.setattr(Redis, ...)`),
  not the copy reachable through the module under test
  (`checks.Redis`). Both patch the same object, but `mypy --strict`'s
  `no_implicit_reexport` rejects the second: a module that merely
  imported a name doesn't export it. Only reach through the module for a
  name it actually defines (`main_module.settings`).

## Don't

- Depend on test execution order, or leave state (files, env vars) for a
  later test to pick up.
- Import across suites (`unit/` ↔ `integration/` ↔ `e2e/`) — each stays
  independently runnable.
