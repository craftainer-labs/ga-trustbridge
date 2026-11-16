# NFR-0029. Tenant isolation is enforced by the database, and rolls out N-1 compatibly in two releases

## Status

Accepted. Release N ("expand": the tenant context, the definer functions and inert policies) is
implemented; `REQUIRED_SCHEMA_VERSION` is still 1. The release N+1 "enforce" migration
(`ENABLE` and `FORCE ROW LEVEL SECURITY` on the tenant tables including `github_environments`, `schema_compat` 2) has not
shipped, so until it does the isolation of the tenant tables rests on the application's tenant
context and not yet on Postgres. `audit_events` is the exception: it is forced from its own
release ([NFR-0031](NFR-0031-audit-log-is-append-only-complete-and-exportable.md)).

## Attribute

Security (tenant isolation, least privilege), deployability.

## Description

- **Isolation in the database.** A tenant is a GitHub installation. For the tenant-owned tables
  (`tenants`, `tenant_repositories`, `scope_leases`, `adapter_resources`, `adapter_credentials`,
  `credential_operations`, the six of [ADR 0031](../adrs/0031-tenancy-and-row-level-security.md),
  and `github_environments`, added later by NG6h-6a with `tenant_isolation` and `owner_all` policies
  created before enforcement), a statement run by the app role
  inside `tenant_context(A)` sees and writes only tenant A's rows. With no context it sees no row
  and every write fails. The app role is neither the owner, a superuser nor `BYPASSRLS`, and
  never owns a tenant-owned table.
- **Context cannot leak.** The context is set only by the transaction-begin hook, is empty again
  after commit or rollback on a pooled connection (including `DB_POOL_MODE=external`), and never
  crosses concurrent tasks. Background loops (relay, reconciler, sweeps, purge) reach tenant rows
  only through `iterate_tenants`, one tenant's context at a time, and one tenant's exception
  stops no other.
- **Definer functions are hardened.** Every `SECURITY DEFINER` function pins its `search_path`
  and ends it with `pg_temp`; `EXECUTE` is granted to the two roles and not to `PUBLIC`.
- **Fail closed at runtime.** A process whose database role is a superuser or has `BYPASSRLS`
  reports not ready once a table is forced, and production refuses to migrate when either role
  is absent or the app role is over-privileged. No migration creates a role.
- **Two-release rollout.** The previous release's code and test suites must run against the
  schema of the next (NFR-0027): release N creates policies while RLS is off, release N+1
  enables and forces it. Skipping release N is unsupported, and N-1 code must be gone from the
  fleet before the N+1 migrate Job runs ([upgrade.md](../upgrade.md)).

## Source

Enterprise baseline rows EB-3 (multi-tenancy with `tenant_id` and Postgres row-level security),
EB-6 (HA: Postgres is the only durable state, so isolation cannot live in a process) and EB-2
(least privilege) of the plan `docs/plans/2026-10-craftainer-labs-apps.md`. Stakeholder: Security
/ compliance. Design: [ADR 0031](../adrs/0031-tenancy-and-row-level-security.md) (invariants 1-17),
[ADR 0028](../adrs/0028-trustbridge-architecture-and-instance-layout.md); rollout rule from
[NFR-0027](NFR-0027-expand-contract-migrations.md).

## Verification

Integration tests run as the app role against a real Postgres, in the Checks workflow's prek job
(pytest, 100% coverage gate); none is a separate CI job.

- Isolation, context and policies (ADR 0031 invariants 1-9, 11, 13, 15, 17):
  `tests/trustbridge/tenancy/test_rls_integration.py`, among them
  `test_in_a_context_only_that_tenants_rows_are_visible_and_foreign_writes_fail`,
  `test_with_no_context_every_table_is_empty_and_every_write_fails`,
  `test_the_setting_is_cleared_when_a_transaction_ends_and_a_pooled_connection_is_reused`,
  `test_concurrent_tenant_sessions_never_see_each_others_context`,
  `test_a_handler_never_inherits_a_transaction_begun_before_its_context`,
  `test_the_app_role_cannot_insert_or_delete_a_tenant_and_updates_only_its_own_row`,
  `test_host_scoped_tables_have_no_rls_and_work_without_a_context`,
  `test_rls_is_forced_and_the_owner_with_its_policy_still_reads_everything`,
  `test_the_release_n_schema_has_policies_but_rls_is_off_so_nothing_is_filtered`,
  `test_a_disabled_policy_or_an_owner_connection_fails_the_isolation_probe`,
  `test_production_refuses_absent_roles_a_bypassing_app_role_or_one_that_owns_a_table`,
  `test_production_is_read_from_mode_and_no_migration_creates_a_role`.
- Iteration (invariants 11-12): `test_iterate_tenants_yields_every_tenant_once_each_inside_its_own_context`
  and `test_one_tenants_exception_does_not_stop_the_others` in the same file;
  `tests/trustbridge/tenancy/test_tenancy_unit.py`.
- Definer hardening (invariant 10): `test_every_definer_function_has_pg_temp_last_in_its_search_path`
  in `tests/trustbridge/tenancy/test_definer_search_path_integration.py`;
  `test_definer_functions_have_a_fixed_search_path_and_ignore_a_shadowing_schema` and
  `test_execute_on_the_functions_is_granted_to_the_two_roles_and_not_to_public` in the RLS file.
- Role safety at runtime (invariant 16): `tests/trustbridge/tenancy/test_role_check_integration.py`
  (`test_a_bypassrls_role_is_not_ready_once_a_table_is_forced`,
  `test_a_superuser_is_not_ready_once_a_table_is_forced`) and `test_role_check_unit.py`.
- Schema gate: `tests/trustbridge/worker/test_schema.py` (a process waits for, and accepts a
  schema at least as new as, its required `schema_compat`).
- N-1 compatibility: `.github/workflows/migration-compat.yml` (PR job) migrates a fresh database
  and runs the latest release's integration and e2e suites against it, and lints new revisions
  with Squawk.

Known gaps: the migration-compat job runs only on PRs that touch `alembic/versions/**`,
`src/app/models/**`, `uv.lock` or its own files, and its N-1 half skips and stays green while no
stable `v*` release tag exists (the repository has none today) or while the latest tag's
`src/app/config.py` predates `RUN_MIGRATIONS_ON_STARTUP`. So invariants 13 and 14 are not yet checked against a real
previous release; the release N+1 enforce migration and the test that N+1 code refuses to start
against `schema_compat` 1 do not exist yet. The same caveat applies wherever [NFR-0031](NFR-0031-audit-log-is-append-only-complete-and-exportable.md)
and [NFR-0034](NFR-0034-tenant-data-deletion-is-resumable-audited-and-complete.md) cite the job.
`github_environments` is covered by the policy-existence list in `test_rls_integration.py` and,
with RLS enabled by hand, by the deletion flow tests (`tests/trustbridge/deletion/test_flow_integration.py`),
but has no isolation test of its own and is not part of the ADR 0031 invariants or of the
enforce migration, which does not exist yet. The compat job's path filter lists `src/app/models/**`
and not `src/trustbridge`, so a model-only change there does not trigger it.
