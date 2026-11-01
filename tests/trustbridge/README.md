# tests/trustbridge/

Tests for the instance package `src/trustbridge/` (instance-added files under the replace-tier
`tests/` survive a template sync; do not edit template-owned test files).

- Unit tests (everything except `test_integration_pg.py` and the subprocess smoke test) use the
  in-memory fakes in `fakes.py` (a stream-capable Valkey client, secret store, host checker, hook
  delivery client) and need no service.
- `worker/test_integration_pg.py` is marked `integration`: it needs the devcontainer stack's
  Postgres and runs in CI's stack.
- `test_main.py` runs the real `trustbridge.main:app` in a subprocess (the entrypoint mutates the
  template app at import, which must not leak into the template's own tests).

Each invariant of ADR 0028/0030 assigned to NG3 is cited in the docstring or a comment of the test
that covers it (`inv. N`).
