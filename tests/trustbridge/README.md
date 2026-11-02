# tests/trustbridge/

Tests for the instance package `src/trustbridge/` (instance-added files under the replace-tier
`tests/` survive a template sync; do not edit template-owned test files).

- Unit tests (everything except the files marked `integration` and the subprocess smoke test) use
  the in-memory fakes in `fakes.py` (a stream-capable Valkey client, secret store, host checker,
  hook delivery client) and need no service. `helpers.make_monitor` builds a real `HostMonitor`
  over an in-memory RSA signer; the `key_signers` fixture (`conftest.py`) swaps the signers a
  `build_host_runtime` would build from `SIGNERS` for that signer.
- `worker/test_integration_pg.py` and `adapters/test_sql_store_integration.py` are marked
  `integration`: they need the devcontainer stack's Postgres and run in CI's stack (the 100%
  coverage gate counts the SQL stores through them).
- `test_main.py` runs the real `trustbridge.main:app` in a subprocess (the entrypoint mutates the
  template app at import, which must not leak into the template's own tests).

Each invariant of ADR 0028/0030 assigned to NG3 is cited in the docstring or a comment of the test
that covers it (`inv. N`).
