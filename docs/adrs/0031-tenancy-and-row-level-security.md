# 0031. Tenancy and row-level security: tenant context, policies and a two-release rollout

## Status

Proposed. For the user's G-api review (NG6a0); no code is written yet. It becomes Accepted
when the implementation (NG6a) merges. Builds on
[0028](0028-trustbridge-architecture-and-instance-layout.md) ("Tenancy", the roles table and
its open issues), which this ADR settles, and is used by
[0029](0029-adapter-plugin-interface.md) (the tables and lease) and by
[0032](0032-audit-log.md) and [0033](0033-admin-api-v1-rbac-and-limits.md). Where this ADR
differs from a sentence in 0028 (database role creation), it says so; 0028's Decision text is
not edited.

## Context

Tenant = GitHub installation (EB-3). Installation ids are unique only per GitHub host, so a
tenant is `(host, installation_id)` and every tenant-owned table carries a surrogate
`tenant_id` UUID (0028 "Tenancy"). 0028 chose Postgres row-level security keyed on a per-
transaction setting and deferred the details here. What exists today (NG3-NG5, migration head
`a7e2c94b1d63`):

1. **Tables.** Tenant-owned: `tenants` (its `id` is the tenant id), `tenant_repositories`,
   `scope_leases`, `adapter_resources`, `adapter_credentials`, `credential_operations` (each
   has a `tenant_id` column). Host-scoped, written before a tenant is known:
   `webhook_deliveries` (primary key `(host, delivery_id)`) and `github_host_registrations`.
   `schema_compat` is a single-row, instance-wide table.
2. **Roles.** The runtime role `trustbridge_app` already exists in migrations only as a name:
   each migration grants to it `if the role exists` (`c3a5e7d91f42`, `5d1f3a7c9e24`). The
   migrate Job runs as `trustbridge_owner` (0028 "Processes and database roles"). 0028 says
   the Job "creates the application role"; the decision below supersedes that sentence.
3. **Tenant resolution runs before a tenant is known.** The worker resolves a delivery's
   `(host, installation_id)` to a tenant id (`TenantResolver.tenant_id_for`,
   `worker/ports.py`), and an `installation` event creates the tenant (`upsert_installation`).
   Both read or write `tenants` with no tenant in hand, so a plain `tenant_id = current` policy
   on `tenants` would break them.
4. **Cross-tenant background duties.** The reconciler sweeps leases, operations and resources
   of every tenant (EB-6). The relay reads only `webhook_deliveries`.
5. **Pooling.** `DB_POOL_MODE=external` (PgBouncer in transaction mode, ADR 0022) is
   supported, so session-level state (`SET`, session advisory locks) is not allowed; the
   setting must be transaction-local.
6. **N-1.** NFR-0027 requires every migration to work with the previous release's code, and the
   `migration-compat` job runs the latest release's suites against the new schema. Turning on
   RLS makes every query from code that does not set the tenant return no rows, so it cannot
   ship in the same release as the code that sets it.
7. **RLS is easy to defeat silently.** A superuser, or a role with `BYPASSRLS`, ignores every
   policy; a table's owner ignores its policies unless the table is `FORCE`d (PostgreSQL
   "Row Security Policies"). A policy referencing an unset setting is the usual way a
   multi-tenant app leaks or locks itself out.

## Decision

### Already decided (cited, not reopened)

- `webhook_deliveries` and `github_host_registrations` stay **outside RLS**: they are
  host-scoped and written before the tenant is known (0028 "Tenancy"). The relay and the
  delivery reconciler read only `webhook_deliveries` and so need no tenant iteration.
- **Two-release, N-1 compatible rollout** (NFR-0027, `migration-compat`): release N ships the
  session hook with the app role not yet RLS-bound; release N+1 enables and forces RLS and
  bumps `schema_compat` / `REQUIRED_SCHEMA_VERSION`. See "Rollout".
- **Roles are created outside migrations.** `trustbridge_app` and `trustbridge_owner` are
  created by the DBA or the Helm chart. Migrations only `GRANT`, never create a login role, and
  **refuse to run in production when a role is absent**. (Elsewhere, for dev, compose and
  tests, the existing "grant if the role exists" behaviour stays; production is the strict
  case.) This replaces the clause in 0028 that has the Job create the application role.

### The tenant context

- A `ContextVar[UUID | None]` named `current_tenant` in `trustbridge.tenancy` holds the
  tenant for the running task. It is set only through the context manager
  `tenant_context(tenant_id)`, which sets and resets it (token-based) and nothing else may
  write it.
- **The session hook.** One `after_begin` listener on SQLAlchemy's `Session` class (the async
  session proxies to it; listeners attach to `AsyncSession.sync_session`'s class,
  SQLAlchemy "ORM Events on AsyncSession"). It runs on the connection the transaction just
  began and executes

  ```sql
  SELECT set_config('app.tenant_id', :tenant_id_or_empty, true)
  ```

  The third argument `true` makes it transaction-local, so it is discarded at commit or
  rollback and never leaks to the next user of a pooled connection, including through
  PgBouncer's transaction mode. The value is the ContextVar's UUID text, or the empty string
  when it is unset. The hook never reads from the request, a header or a claim.
- **Fail closed.** The policies compare against `tb_current_tenant()`, a `STABLE` SQL function
  returning `NULLIF(current_setting('app.tenant_id', true), '')::uuid`. An unset or empty
  setting is `NULL`, `tenant_id = NULL` is never true, so a missing context sees no rows and can
  write none (`WITH CHECK` fails). Policies call it as `(SELECT tb_current_tenant())` so
  Postgres evaluates it once per statement, not per row.
- **Who sets it.** The worker, after resolving the delivery's tenant, runs its handlers inside
  `tenant_context`. The admin API sets it in a dependency for a route with a `{tenant_id}`
  path parameter, after the RBAC check (0033) and not before. Background duties use
  `iterate_tenants` (below). Code with no tenant (host-scoped tables, resolution) runs
  outside any context and uses only host-scoped tables or the functions below.

### Policies

Enabled and forced on `tenants`, `tenant_repositories`, `scope_leases`, `adapter_resources`,
`adapter_credentials` and `credential_operations`. For each table other than `tenants`:

```sql
CREATE POLICY tenant_isolation ON <table>
    FOR ALL TO trustbridge_app
    USING (tenant_id = (SELECT tb_current_tenant()))
    WITH CHECK (tenant_id = (SELECT tb_current_tenant()));
```

`tenants` gets the same shape keyed on `id`, for `SELECT` and `UPDATE` only: the app role
cannot `INSERT` or `DELETE` tenants directly. Every tenant-owned table also gets
`CREATE POLICY owner_all ... TO trustbridge_owner USING (true) WITH CHECK (true)`, because
`FORCE ROW LEVEL SECURITY` subjects the owner too and the migrate Job and the functions below
run as the owner. The app role gets table-level grants only as today; no `TRUNCATE`, no DDL.

**Why `FORCE`.** The tables are owned by `trustbridge_owner`; the app role does not own them,
so it is already subject to RLS. `FORCE` defends against an operator accidentally running the
app as the owner. It is cheap and the owner policy above keeps the Job working.

**`tenants` and resolution: three narrow functions.** Resolution and tenant creation happen
with no tenant in hand (Context 3). Rather than a `BYPASSRLS` role or a setting that any
query can flip, three `SECURITY DEFINER` functions owned by `trustbridge_owner`, each with a
fixed `search_path = pg_catalog, public`, are the only unscoped paths to `tenants`, and the app
role has `EXECUTE` on them and nothing else on that table outside its own row:

| Function | Does |
| --- | --- |
| `tb_resolve_tenant(host, installation_id) -> uuid` | the tenant id of a pair, or `NULL` |
| `tb_upsert_tenant(host, installation_id, org, org_id, state) -> uuid` | the `installation` event's create-or-update; never touches the admin-disable columns (0033, tenant disable) |
| `tb_list_tenants() -> setof (id, host, installation_id, state)` | what `iterate_tenants` and an instance-scoped admin list read |

A function is a deliberate, greppable, parameter-checked gap; the alternative (a setting such
as `app.tenant_scope = 'all'` that a policy honours) lets any code path, including a bug, flip
it. The accepted limit: RLS here defends against **missing tenant filters and wrong-tenant
writes in code we trust**, not against a fully compromised application process, which can
call these functions and sets its own context anyway.

### `iterate_tenants` for relay, reconciler and purge

`iterate_tenants(session_factory)` is an async generator in `trustbridge.tenancy`: it reads
`tb_list_tenants()` once outside any context (a short transaction, closed before work starts),
then yields each tenant id inside `tenant_context`, so work for one tenant never runs with
another's context. Rules:

- The relay and the delivery reconciler do **not** iterate: they read only the host-scoped
  `webhook_deliveries` (0028 Decision, "Open issues").
- The lease sweep, the operation resumption, the orphan sweep and the parked-value sweep
  (0028 invariants 22-23) iterate tenants. A failure for one tenant is logged and counted
  (`tenant_sweep_failures`) and the loop continues with the next; a tenant that fails does not
  starve the others, and the next pass retries it.
- Each tenant's work is its own transaction(s), so a long sweep never holds one transaction
  across tenants.
- The webhook body purge and the audit purge (0032) are host- or instance-scoped and do not
  iterate. EB-15 data deletion per tenant is not part of this ADR (0033, Decision needed 9).
- A tenant that is `suspended`, or admin-disabled (0033), is still iterated by the lease and
  orphan sweeps (they release resources and finish in-flight operations) but starts no new
  adapter operation.

### Rollout: N-1 compatible in two releases

| | Release N ("expand") | Release N+1 ("enforce") |
| --- | --- | --- |
| Code | `tenant_context`, the `after_begin` hook, every tenant-owned access inside a context, the three functions' callers, `iterate_tenants` | requires `REQUIRED_SCHEMA_VERSION = 2` |
| Migration | creates `tb_current_tenant`, the three functions and the policies **while RLS is still disabled** on the tables (so the policies exist but filter nothing); grants `EXECUTE`; writes `schema_compat` 1 (`GREATEST`, no bump) | `ALTER TABLE ... ENABLE` and `FORCE ROW LEVEL SECURITY` on the six tables; bumps `schema_compat` to 2 |
| App role | not RLS-bound: queries behave as before | RLS-bound |
| Why safe | N-1 code never sets the setting and, with RLS off, does not need to | N code (previous release) sets the hook and so works against the enforced schema; the `migration-compat` job runs release N's suites against it and proves that |

Rolling back N+1 code to N is safe for the same reason (N sets the hook, and accepts a
`schema_compat` of 2 or more, 0028 invariant 13). The gap this leaves: running **N-1** code
after the N+1 migration would see no rows. N+1 code requires `schema_compat` 2, so it waits
for the migration, and the release notes of N+1 state that N-1 must be fully rolled out of the
fleet before the N+1 migrate Job runs. Skipping release N is not supported.

### Role safety checks

- Migrations in production refuse to run if `trustbridge_app` or `trustbridge_owner` is
  absent, or if `trustbridge_app` is a superuser, has `BYPASSRLS`, or owns any of the six
  tables (these would silently defeat RLS).
- The api, worker and reconciler check at startup, once the schema's `schema_compat` is 2 or
  more, that `current_user` is neither a superuser nor `BYPASSRLS`. If it is, the process
  reports not ready with the reason and, in production, exits after `SCHEMA_WAIT_SECONDS`, like
  any other unmet schema condition. (Before the enforcing release the check is skipped.)

### Decisions this ADR records that the user can overrule

These are design choices made while writing, not user decisions yet; they are listed here so
G-api sees them next to the Decision needed items below.

- `SECURITY DEFINER` functions for the unscoped paths instead of a `BYPASSRLS` role or a
  scope-widening setting.
- `FORCE` plus an explicit owner policy.
- The `tenants` table inside RLS (rather than treating it as a host-scoped registry
  like `webhook_deliveries`). It holds no secret, only host, installation id, org and state, so
  leaving it outside RLS and skipping the functions would be simpler; it is kept inside so
  that one admin-API bug cannot list other tenants, and the functions are the price.

## Invariants the implementation must test

Each is one testable sentence; NG6a implements and tests them against a real Postgres with the
app role (not the owner, not a superuser).

**Context and hook**

1. Inside `tenant_context(A)`, a query on each of the six tables returns only tenant A's rows
   and an `INSERT` or `UPDATE` that sets another tenant's id fails.
2. With no tenant context, a query on each of the six tables returns no rows and every write
   fails; it never returns another tenant's rows and never raises a connection-state error
   that hides the failure.
3. After a transaction ends (commit and rollback), the next transaction on the same pooled
   connection sees an empty `app.tenant_id` unless its own context sets one, and this holds
   with `DB_POOL_MODE=external`.
4. Two concurrent tasks (and two concurrent admin requests) with different tenants never see
   each other's context, including across `await` points and threadpool hops.
5. The hook is the only writer of `app.tenant_id`, `tenant_context` resets the ContextVar on
   exit including when the body raises, and a request or event field never reaches the hook
   without passing the RBAC or resolution step first.

**Policies and functions**

6. The app role cannot `INSERT` or `DELETE` a `tenants` row directly, can read and update only
   its context's row, and can create or resolve a tenant only through the functions.
7. `tb_upsert_tenant` never changes the admin-disable columns (0033) and `tb_resolve_tenant`
   returns `NULL` for an unknown pair.
8. `webhook_deliveries` and `github_host_registrations` have RLS disabled and the app role reads
   and writes them with no context set; the relay and the delivery reconciler never set one.
9. `FORCE ROW LEVEL SECURITY` is set on the six tables, and the owner role, with the owner
   policy, can still read all rows (the migrate Job and the functions work).
10. A `SECURITY DEFINER` function has a fixed `search_path`, and an unqualified object shadowed
    in a schema on the caller's `search_path` is not used.

**Iteration**

11. `iterate_tenants` yields every tenant once, runs each tenant's body inside that tenant's
    context only, and one tenant's exception does not stop the others or leak its context to
    the next.
12. The lease sweep, operation resumption and orphan sweep read no other tenant's rows while
    working on one (a test with two tenants and a planted foreign row).

**Rollout and roles**

13. With the release-N migration applied and RLS not yet enabled, the previous release's
    integration suite passes against the schema (policies exist and filter nothing).
14. With the release-N+1 migration applied, release N's integration suite passes against it
    (`migration-compat`), and release N+1 code refuses to start against `schema_compat` 1.
15. In production a migration refuses to run when either role is absent, and when
    `trustbridge_app` is a superuser, has `BYPASSRLS` or owns a tenant-owned table; no
    migration issues `CREATE ROLE`.
16. A runtime process whose database role is a superuser or has `BYPASSRLS` reports not ready
    once RLS is enforced, with the reason in its readiness detail.
17. A test that disables a policy (or connects as the owner without `FORCE`) fails the
    isolation tests, so a silently inert policy is caught.

## Decision needed

None. The user-facing choices for this item are in
[0033](0033-admin-api-v1-rbac-and-limits.md) (API shape, roles, tenant disable, deletion).

## Open issues

- **Operators who bring their own database roles.** A DBA who maps IdP or LDAP roles onto the
  two names must keep `trustbridge_app` non-superuser and non-`BYPASSRLS`; the runtime check
  catches it, but the Helm chart's values (NG7) must document it.
- **Cost.** `(SELECT tb_current_tenant())` inside policies and an index starting with
  `tenant_id` on each table are needed for the planner to use the policy as an index
  condition; NG6a checks the plans of the lease and sweep queries and adds indexes through an
  expand migration if a scan appears.
- **Per-tenant host-scoped data.** `webhook_deliveries` bodies contain one tenant's payload but
  are not tenant-isolated. The admin API serves them only to instance-scoped roles (0033).
  If a tenant-scoped role ever needs them, the tenant id would have to be stored on the row
  after resolution; not part of this ADR.
- **`tb_list_tenants` at scale.** It returns every tenant to every sweeper replica; with
  thousands of tenants a sharded sweep (by hash of tenant id) is a later optimisation.
- **Replication and backups.** Restoring one tenant from a backup is not supported by this
  design (EB-6 documents whole-database backup and restore).

## Consequences

- A forgotten `WHERE tenant_id = ...` in tenant-scoped code returns nothing instead of leaking
  another tenant's data; the cost is that every new code path must run inside a context, and
  a missing one looks like "no data", which tests (invariant 2) and a debug log of the empty
  context are meant to make visible.
- The two-release rollout means RLS is not enforced until release N+1; between the two,
  isolation rests on the application's own filters, as it does today.
- Three `SECURITY DEFINER` functions are new privileged surface. They are small, take no
  free-form SQL and are covered by invariants 6, 7 and 10.
- Tests for tenant-owned tables need a real Postgres and the app role; fakes cannot show RLS.
  The existing memory twins stay for unit tests and gain a context check of their own.
- A superuser-run dev database makes RLS a no-op; dev compose therefore creates and uses the
  two roles, and the runtime check refuses a bypassing role in production.

## Sources

- PostgreSQL row security policies (policies, `FORCE`, owner and `BYPASSRLS` bypass,
  `USING` vs `WITH CHECK`): https://www.postgresql.org/docs/current/ddl-rowsecurity.html
- `set_config(setting, value, is_local)` and `current_setting(name, missing_ok)`:
  https://www.postgresql.org/docs/current/functions-admin.html
- `CREATE FUNCTION ... SECURITY DEFINER` and the `search_path` guidance:
  https://www.postgresql.org/docs/current/sql-createfunction.html
- SQLAlchemy ORM events, `SessionEvents.after_begin(session, transaction, connection)` and
  listening on `AsyncSession.sync_session` (context7 `/websites/sqlalchemy_en_20_orm`):
  https://docs.sqlalchemy.org/en/20/orm/events.html,
  https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html
- Python `contextvars`: https://docs.python.org/3/library/contextvars.html
- ADR [0028](0028-trustbridge-architecture-and-instance-layout.md) (tenancy, roles, open
  issues), ADR 0022 (pool modes), NFR-0027
  (`docs/nfrs/NFR-0027-expand-contract-migrations.md`), the plan's EB-3 and EB-6 rows.
- Code read: `src/trustbridge/worker/models.py`, `worker/ports.py`, `worker/schema.py`,
  `alembic/versions/c3a5e7d91f42_*.py`, `5d1f3a7c9e24_*.py`, `a7e2c94b1d63_*.py`.
