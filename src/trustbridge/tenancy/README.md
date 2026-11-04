# trustbridge/tenancy/

Tenant context and the per-transaction row-level-security setting
([ADR 0031](../../../docs/adrs/0031-tenancy-and-row-level-security.md)). This is **release N** of
the two-release rollout: the hook and the policies exist, but RLS is not enabled on any table
yet (release N+1), so the app role behaves as before.

- `context.py`: `current_tenant` (a `ContextVar[UUID | None]`) and `tenant_context(tenant_id)`,
  the only writer of it (token-based, reset on exit even when the body raises).
- `hook.py`: `install_tenant_hook()` listens for `after_begin` on SQLAlchemy's `Session` class
  (the async session proxies to it) and runs `set_config('app.tenant_id', <uuid or ''>, true)`
  on the connection the transaction just began. `true` makes it transaction-local, so it never
  leaks to the next user of a pooled connection, PgBouncer transaction mode included. It is the
  only writer of the setting. `worker.runtime.build_core` and `main.install` install it.
- `session.py`: `tenant_session(factory, tenant_id)` (a session opened inside the context),
  `list_tenants` (reads `tb_list_tenants()` with no context), `iterate_tenants` (each tenant id
  yielded inside its own context) and `for_each_tenant` (runs a coroutine per tenant, logs and
  counts a failure as `trustbridge.tenants.sweep_failures` and carries on).
- `role_check.py`: the runtime role safety check. `evaluate(RoleFacts)` is the pure decision: not
  ready only when an RLS table is forced (the six tenant-owned tables or `audit_events`) and
  `current_user` is a superuser or has `BYPASSRLS`; with nothing forced yet it is skipped.
  `RoleGate` reads the role and the forced tables with one catalog query (fail closed if it cannot),
  `RoleCheck` / `register_role_check` add a `role_safety` check next to the schema check on the api's
  `/ready`, with the reason as its detail. See "Role safety check" below.

## Rules

- The hook runs only when a transaction **begins**. Open the session inside the context, and
  run any RBAC or resolution query in its own session and transaction, closed before the context
  is entered; a session that began a transaction earlier keeps that transaction's (empty)
  setting.
- The tenant id comes from a resolved tenant, never from a request field, header or claim.
- Tenant-owned tables are read and written inside a context; with none, the policies (once
  enabled) return no rows and reject every write. Host-scoped tables (`webhook_deliveries`,
  `github_host_registrations`) are outside RLS and need none; the relay and the delivery
  reconciler never set one and never iterate.
- `ThreadPoolExecutor` hops via `loop.run_in_executor` do not copy the context; `asyncio.to_thread`
  does.
- Use `for_each_tenant` for a background sweep. A hand-written `async for` over
  `iterate_tenants` must wrap it in `contextlib.aclosing` so the context is reset deterministically.

## Database side (migration `d4b8f1e60a27`)

`tb_current_tenant()`, the `SECURITY DEFINER` functions `tb_resolve_tenant`, `tb_upsert_tenant`
and `tb_list_tenants` (fixed `search_path = pg_catalog, public`, `EXECUTE` revoked from `PUBLIC` and
granted to `trustbridge_app` and `trustbridge_owner`), and the inert `tenant_isolation` and
`owner_all` policies. `tb_delete_tenant` arrives with tenant data deletion (NG6h), and
`tb_list_tenants` gets its real `admin_disabled_at` and `deleting` values from NG6e and NG6h.

## Grants

Production table and function grants for `trustbridge_app` come only from the migrations
(`GRANT` to roles that already exist, never `CREATE ROLE`), per ADR 0031. The root `compose.yml`
smoke stack additionally sets default privileges for the app role; that is smoke-only, because the
template's frozen migrations grant nothing and the perf run needs CRUD on the template's tables.

## Role safety check

The api, the worker and the reconciler check once the schema is current that `current_user` is
neither a superuser nor `BYPASSRLS` (ADR 0031 "Role safety checks"). The api starts it after its
schema wait (`main.wait_gates`); the worker and the reconciler share `worker.runtime.run_process`.

| | api | worker / reconciler |
| --- | --- | --- |
| `MODE=production` | `/ready` not ready with the reason; exits 1 after `SCHEMA_WAIT_SECONDS` | waits, then exits 1 after `SCHEMA_WAIT_SECONDS` |
| otherwise | `/ready` not ready with the reason, a warning is logged, polling continues, never exits | one check, a warning is logged, processing continues |

The seven RLS tables are looked up schema-qualified (`role_check.APP_SCHEMA`, `public`) with
`to_regclass`, as plain or partitioned tables, never through the `search_path`. An expected table
that cannot be found is itself not ready (the reason names it), so a superuser is never passed by
not seeing the tables. The schema wait and the role wait share one `SCHEMA_WAIT_SECONDS` budget
counted from start-up.

Dev databases are often reached as a superuser, so outside production the check only reports.
`MODE=mock` is always ready and touches no database.
