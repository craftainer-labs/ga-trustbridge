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
  only writer of the setting. `worker.runtime.build_core` and `main.install` install it. The same
  call also installs a second `after_begin` listener, the transaction time bound (below), configured
  from `TRANSACTION_TIMEOUT_SECONDS`.
- `session.py`: `tenant_session(factory, tenant_id)` (a session opened inside the context),
  `list_tenants` (reads `tb_list_tenants()` with no context), `get_tenant` (one tenant by id through `tb_get_tenant()`, also with no context), `iterate_tenants` (each tenant id
  yielded inside its own context) and `for_each_tenant` (runs a coroutine per tenant, logs and
  counts a failure as `trustbridge.tenants.sweep_failures` and carries on).
- `role_check.py`: the runtime role safety check. `evaluate(RoleFacts)` is the pure decision: not
  ready only when an RLS table is forced (the six tenant-owned tables or `audit_events`) and
  `current_user` is a superuser or has `BYPASSRLS`; with nothing forced yet it is skipped.
  `RoleGate` reads the role and the forced tables with one catalog query (fail closed if it cannot),
  `RoleCheck` / `register_role_check` add a `role_safety` check next to the schema check on the api's
  `/ready`, with the reason as its detail. See "Role safety check" below.
- `timeout_check.py`: the transaction time bound check (ADR 0034 B8). `evaluate(TimeoutFacts, lag)` is the
  pure decision, `TimeoutGate` / `TimeoutCheck` / `register_timeout_check` mirror the role check and add a
  `transaction_timeout` check on `/ready`. See "Transaction time bound" below.

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
and `tb_list_tenants` (fixed `search_path = pg_catalog, public, pg_temp` since `6f3b9d2e4a85`, `EXECUTE` revoked from `PUBLIC` and
granted to `trustbridge_app` and `trustbridge_owner`), and the inert `tenant_isolation` and
`owner_all` policies. `tb_delete_tenant` arrives with tenant data deletion (NG6h-1, migration
`9c2e5a7f1d84`, below), and
`tb_get_tenant(uuid)` (migration `a1c4e8d27b53`, NG6d-2) is its single-row twin with the same columns.
Migration `b7d3f5a19c68` (NG6e-1) adds the nullable `tenants.admin_disabled_at`, `admin_disabled_by` and
`admin_disable_reason` columns (a CHECK keeps them all set or all null; `tb_upsert_tenant` and the sync never
write them), drops and recreates both read functions to return them (a function's row type cannot change
with `CREATE OR REPLACE`; the readers select named columns, so it is N-1 safe) and adds
`tb_set_tenant_disabled(id, by, reason, disable)`, a definer function that makes the disable or enable one
row-locked write and returns the before and after values. A repeat disable keeps the first time, actor and
reason. `TenantInfo` has the two new fields (defaulted, last). `deleting` was the constant `false`
until migration `9c2e5a7f1d84` (below), which makes it real and adds the refusal to enable a tenant
with a non-terminal deletion at the `v_deleting` hook in `tb_set_tenant_disabled`.

Migration `9c2e5a7f1d84` (NG6h-1, ADR 0034) is the database foundation of tenant data deletion. Only
`SqlTenants.upsert_installation` and `MemoryTenants` (and so the installation handlers) use it so
far. It adds the host-scoped `tenant_deletions` table (outside RLS, no foreign key to `tenants`;
states `requested`, `draining`, `platform`, `github`, `database`, `done`, `failed`, `cancelled`, of
which the last three are terminal; one non-terminal row per tenant). The app role has `SELECT` only:
every write goes through a `SECURITY DEFINER` function running as the owner role. It also adds
`webhook_deliveries.tenant_id` (nullable, partial index, `deleted` status). It replaces, under the
same signatures, `tb_list_tenants` and `tb_get_tenant` (`deleting` is now real),
`tb_set_tenant_disabled` (`enable` is refused with SQLSTATE 55000 while a non-terminal deletion
exists; the state is read after the tenant row lock) and `tb_upsert_tenant` (a `deleted` tenant is
returned unchanged; a `done` deletion of `(host, installation_id)`, or a `database`-step deletion
whose tenant row is already gone, is a tombstone, so the function creates nothing; it returned NULL here, and `3b8d6f2a4c17` raises instead).
`SqlTenants` and `MemoryTenants` raise `TenantDeletedError` (reason `tombstone`, `deleting` or
`deleted`) instead, and the installation handler drops the event; a new installation id is
unaffected. `tb_upsert_tenant`, `tb_delete_tenant` and `SqlTenants.upsert_installation` first take
`pg_advisory_xact_lock(hashtextextended('tb_tenant:' || host || ':' || installation_id, 0))`, so a
concurrent delete and upsert cannot recreate the tenant. `tb_request_tenant_deletion(...)` takes the
same row lock as enable and disable, validates the confirmation before returning an open deletion,
and stores an optional `not_before` (default NULL: no delay). `tb_delete_tenant(uuid, text, bigint)`
(holder and fence, NG6h-4a-1, migration `5d7a2c8e1b94`) refuses (55000) unless the tenant is
admin-disabled, has a deletion in its `database` step whose `not_before` has passed, and has no
rows left in the five other tables or an uncleared `webhook_deliveries` row (`status <> 'deleted'`, migration
`6f3b9d2e4a85`), and raises `TB003` (`deletion lease lost`) unless the caller
holds that deletion's live lease (holder, fence and an unexpired `expires_at`). Because those tables have no
foreign key, a writer is not blocked by the row lock; `tb_delete_tenant` checks again after the
delete and rolls it back if a row committed in between, and the engine must stop writing for a
deleting tenant first. The downgrade is refused while any `tenant_deletions` row or `deleted`
delivery exists. N-1 safe: only additions and same-signature function bodies; the previous release
never reads or writes the new column, status or table.

Every `SECURITY DEFINER` function pins `search_path = pg_catalog, public, pg_temp` (`pg_temp` last, ADR 0031
invariant 10; `6f3b9d2e4a85` added it to the older ones with `ALTER FUNCTION ... SET search_path`);
`tests/trustbridge/tenancy/test_definer_search_path_integration.py` fails CI for a definer without it, so a new
definer must carry it. In `tb_advance_tenant_deletion` a same-state advance whose cursor changes also resets
`attempts`, `retry_at` and `last_error` (a step change always does); an unchanged cursor leaves them alone.

The engine's writes to `tenant_deletions` are `SECURITY DEFINER` functions of `5d7a2c8e1b94` (the
app role keeps `SELECT` only, `search_path` pinned to `pg_catalog, public, pg_temp`): `tb_claim_tenant_deletion` (lease, fence bump,
skips terminal, future `not_before` and backed-off rows, `SKIP LOCKED`), `tb_renew_`, `tb_release_`,
`tb_retry_` (records `last_error`, counts `attempts`, sets `retry_at`), `tb_advance_` (one step along
`requested`, `draining`, `platform`, `github`, `database`), `tb_fail_` (`failed`, always with the lease),
`tb_operator_fail_` (`failed` by an operator: no lease of its own, refused with `TB003` while a live
one is held; the actor lands in `last_error`; granted to `trustbridge_app`, the only runtime role
(ADR 0031), which also serves the admin API), `tb_cancel_` (`requested` only) and `tb_finish_` (`done`,
the tombstone, only after the tenant row is gone). Every guarded call needs the live lease, judged by `clock_timestamp()` (`TB003`);
a terminal or invalid move is `TB004`. None writes `audit_events`: the engine does, in the same
session transaction.

Migration `3b8d6f2a4c17` (NG6h-2) replaces `tb_upsert_tenant` once more (same signature, expand-only):
it never returns NULL any more. A tombstone raises SQLSTATE `TB001` (`tenant tombstoned`) and a
tenant with a non-terminal deletion raises `TB002` (`tenant deletion in progress`), writing nothing:
the DB-level twins of the `tombstone` and `deleting` reasons. `TB` is a user-defined SQLSTATE class,
so the codes cannot collide with PostgreSQL's; a `deleted` tenant row still returns its id
unchanged. `SqlTenants.deleted_error(host, installation_id, error)` maps a `DBAPIError` carrying one
of them to `TenantDeletedError` (otherwise None). `tb_upsert_tenant` has no application caller yet
(`SqlTenants.upsert_installation` does the same upsert under the same advisory lock in SQLAlchemy);
its callers are the integration tests (`test_deletion_db_integration.py`, `test_rls_integration.py`,
`test_reads_integration.py`) and any operator running it by hand, and each of them handles the
errors. `tb_set_tenant_disabled` needs no change (a deletion needs a disabled tenant, enable is
refused while one is open, and a repeated disable keeps the first values). The downgrade restores the
`9c2e5a7f1d84` body (NULL for a tombstone).

`gate.py`'s `SqlTenantGate.allows(tenant_id, kind, deletion_id=None)` (the adapter core's `OperationGate`) refuses every new adapter call for an
admin-disabled tenant except an `OperationKind.DELETE` carrying the `deletion_id` of a non-terminal deletion of the same tenant
(`tenant_deletions`, read in the same statement as the disable flag; a user's delete has no id; ADR 0034 B10, NG6h-4a-3). `SqlTenantGate.standing(host, installation_id)` returns `TenantStanding` (`standing.py`, kept here so `tenancy` never imports the worker: tenant id,
disable flag, and a `deleting` or `tombstone` refusal) from `tb_get_tenant` and `tenant_deletions` in
one statement (`tb_get_tenant` is `STABLE`, so it reads the statement's snapshot and the three
answers agree). The tombstone rule exists in three places (`tb_upsert_tenant`, `SqlTenants._standing`
and `gate.py`); an integration test asserts they agree for every deletion state.

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

## Transaction time bound

The audit export (ADR 0032 "Export") returns only events older than `AUDIT_EXPORT_LAG_SECONDS`, so a
SIEM that resumes from the last `seq` does not skip a late commit. That holds only if no
audit-writing transaction stays open longer than the lag. ADR 0034 B8 enforces it in the
application, for every `Session` of the api, the worker and the reconciler:

- **The hook** (`hook._set_transaction_timeout`, installed with the tenant hook) runs
  `set_config('transaction_timeout', '<ms>ms', true)` (that is `SET LOCAL`: it ends with the
  transaction and never reaches the next user of a pooled connection, PgBouncer transaction mode
  included) right after the transaction begins. It sets the lesser of the session's current value
  (a role-, database- or server-level setting) and the configured bound, treating 0 as off, so it
  never loosens a stricter existing bound.
- **The value** is its own setting, `TRANSACTION_TIMEOUT_SECONDS` (default 15; Helm
  `trustbridge.audit.transactionTimeoutSeconds`), validated at start-up to be above 0 and below
  `AUDIT_EXPORT_LAG_SECONDS` (default 30). It is not derived from the lag, because the lag is the
  consumer-facing guarantee and the timeout is an operational limit that also has to cover the
  slowest legitimate transaction; an explicit value is auditable, and the validation (like
  `ADAPTER_CALL_TIMEOUT_SECONDS` below `LEASE_TTL_SECONDS`) keeps the two apart. Raising the lag
  does not silently loosen the bound, and a bound at or above the lag is a start-up error.
- **Legitimate long transactions.** Every transaction of these processes is short by design: the
  deletion store's calls are one `_tx` each, the audit purge is one batch per transaction
  (`purge_expired`, at most 10000 rows), the export reads one page per transaction, and the deletion
  steps' batches are each their own transaction (ADR 0034 step 5). `DELETION_STEP_PASS_SECONDS` bounds
  a *pass* made of several transactions, not one transaction, so a batch must still finish inside
  `TRANSACTION_TIMEOUT_SECONDS`: size a batch (`AUDIT_PURGE_BATCH_SIZE`) so it does. Migrations are
  outside the bound: the migrate Job (`worker.migrate`) and Alembic use a bare connection as the owner
  role and open no `Session`, so the hook never fires for them (a long DDL transaction there is not
  aborted).
- **The check** (`timeout_check.py`) reads what the database actually applies, inside a session so
  the hook has run (`pg_settings`, in milliseconds), and compares it with the lag: with
  `transaction_timeout` it must be above 0 (0 is "off") and below the lag. It fails when the hook is
  not installed, when the lag was raised past the bound or when the server ignores the setting. It
  mirrors the role check:

| | api | worker / reconciler |
| --- | --- | --- |
| `MODE=production` | `/ready` fails the `transaction_timeout` check with the reason; exits 1 after `SCHEMA_WAIT_SECONDS` | waits, then exits 1 after `SCHEMA_WAIT_SECONDS` |
| otherwise | not ready with the reason, a warning is logged, polling continues, never exits | one check, a warning is logged, processing continues |

- **Before PostgreSQL 17** there is no `transaction_timeout` (the hook cannot set it; it would be an
  unknown parameter). **Production requires
  PostgreSQL 17 or later: the check fails closed there.** Outside production the fallback is
  `statement_timeout` plus `idle_in_transaction_session_timeout` (ADR 0032 allows either): the hook
  sets both to the bound with `SET LOCAL` (each keeping a stricter existing value), and the check accepts them when both are above 0
  and below the lag, **with a warning**, because a transaction of many short statements can still
  outlive the lag. If either is off or not below the lag, it fails as above. The server version
  is the one the dialect read on connect (`server_version_info`); an unknown version takes the
  fallback. Upgrade to PostgreSQL 17 or later for the full guarantee (`docs/upgrade.md`).
