# tests/trustbridge/

Tests for the instance package `src/trustbridge/` (instance-added files under the replace-tier
`tests/` survive a template sync; do not edit template-owned test files).

- Unit tests (everything except the files marked `integration` and the subprocess smoke test) use
  the in-memory fakes in `fakes.py` (a stream-capable Valkey client, secret store, host checker,
  hook delivery client) and need no service. `helpers.make_monitor` builds a real `HostMonitor`
  over an in-memory RSA signer; the `key_signers` fixture (`conftest.py`) swaps the signers a
  `build_host_runtime` would build from `SIGNERS` for that signer.
- `worker/test_health.py` covers the worker and reconciler health endpoints (real sockets on
  loopback, the heartbeat limits, the setting) and needs no service.
- `worker/test_integration_pg.py` and `adapters/test_sql_store_integration.py` are marked
  `integration`: they need the devcontainer stack's Postgres and run in CI's stack (the 100%
  coverage gate counts the SQL stores through them).
- `tenancy/test_rls_integration.py` is marked `integration` (it needs the stack's Postgres, with the
  owner and app roles); `tenancy/test_tenancy_unit.py` needs no service.
- `audit/test_audit_integration.py` is marked `integration` (it needs the stack's Postgres, with the
  owner and app roles); `audit/test_audit_unit.py` needs no service.
- `audit/test_validation_unit.py`, `test_emit_unit.py` and `test_credentials_unit.py` need no
  service (value validation, redaction, the sinks and the credential event derivation);
  `audit/test_audit_integration.py` also holds the emitter tests that need the real table
  (null-tenant denial rows, same-transaction credential events, the canary check).
  `rbac/test_denial_audit.py`, `github/test_tenant_sync_audit.py` and
  `adapters/test_orphan_audit.py` cover the three call-site hooks with fake sinks.
- `admin/` covers the admin API: `test_routes_unit.py` (guards, errors, pagination, export over
  fakes), `test_reads_unit.py` (the tenant lists, deliveries, adapters and the invariant 8 canary,
  one audit event), `test_queries_unit.py` and `test_access_unit.py` (the reads over a scripted
  session), `test_spec_unit.py` (the OpenAPI snapshot drift test), `test_limits_unit.py`,
  `test_cursor_unit.py`, `test_wiring_unit.py`; `test_reads_integration.py` (marked `integration`:
  the SQL of the tenant lists, deliveries, `tb_audit_get` and `tb_get_tenant` against real RLS);
  and `test_admin_integration.py` (marked `integration`, reuses `tenancy/test_rls_integration.py`'s scratch
  database): the tenant context under the async-generator dependency, denial rows and the export
  against real RLS. `audit/test_throttle_unit.py` covers the denial throttle.
- `deletion/` covers the deletion engine: `test_engine_unit.py`, `test_drain_unit.py` and
  `test_audit_unit.py` over the fakes in `deletion/fakes.py` (an in-memory store applying the
  definer functions' transition rules), `test_sql_unit.py` (the SQL sent and the transaction
  boundaries over a scripted session) and `test_sql_integration.py` (marked `integration`: the
  engine over the real functions and RLS, including `completed` after the tenant row is gone).
- `test_main.py` runs the real `trustbridge.main:app` in a subprocess (the entrypoint mutates the
  template app at import, which must not leak into the template's own tests).

Each invariant of ADR 0028/0030 assigned to NG3 is cited in the docstring or a comment of the test
that covers it (`inv. N`).
