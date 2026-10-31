# tests/bench/

`pytest-benchmark` microbenchmarks of the app's own code paths -- the fifth,
independent suite alongside `../unit/`, `../integration/`, `../e2e/` and
`../perf/` (see `../README.md`). Needs no network, database or stack service:

- `test_oidc_bench.py` -- `app.oidc.decode_bearer_token` verifying an RS256
  token. `conftest.py`'s `bearer_token` fixture generates the key and primes a
  `PyJWKClient`'s JWKS cache with its JWK, so nothing is ever fetched.
- `test_asgi_bench.py` -- in-process `httpx.ASGITransport` round trips against
  the real `app.main.app` wired as under `MODE=mock` (`GET /health/live`, and a
  crud list), isolating framework and app overhead from Postgres. The
  `mock_app_client` fixture applies what `MODE=mock` changes for a request
  (crud's in-memory repository, `settings.mode`), overrides auth with fixed
  claims, and restores everything afterwards.

Benchmarks of the extracted packages (`craftainer_crud`,
`craftainer_observability`) belong in those packages' own repos, not here.

Not collected by a plain `pytest` run (`pyproject.toml`'s `addopts` ignores
this directory); run it explicitly, without coverage (a benchmark run is not
a coverage run):

```bash
uv run pytest tests/bench --no-cov
```

It also runs under CodSpeed (`--codspeed`), which replaces the
pytest-benchmark measurement with its own.

## Do

- Keep each benchmark to one operation, with setup in a fixture, so the
  measured callable is only the code under test.
- Keep the suite network- and service-free.

## Don't

- Import from `../unit/`, `../integration/` or `../e2e/` -- the suites stay
  independent.
- Fetch over the network (the JWKS client's URI is unresolvable on purpose).
