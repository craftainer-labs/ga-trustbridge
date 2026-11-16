# NFR-0034. Tenant data deletion is write-ahead, resumable, audited and complete

## Status

Accepted. Implemented ([ADR 0034](../adrs/0034-tenant-data-deletion.md), NG6h).

## Attribute

Compliance (data erasure, SOC 2 / ISO 27001 enablers), reliability, auditability.

## Description

- **Preconditions.** A deletion is requested only by an instance-scoped `admin`, for a tenant
  that is admin-disabled, with a confirmation echoing host, installation id and organisation
  login (verified, never stored). A repeat request returns the open record and starts no second
  deletion; `enable` is refused while a deletion is open.
- **Write-ahead and resumable.** The deletion record is committed before any destructive step and
  survives the removal of the `tenants` row. Every step is idempotent: a crash at any point,
  followed by another reconciler pass, ends in `done` with the same final state. Two reconcilers
  never run one deletion at once (lease and fence), and a holder whose lease lapsed cannot commit.
- **Complete.** After `done`, no row with the tenant's id remains in the tenant-keyed tables and
  the `tenants` row is the last one removed; stored webhook bodies for the tenant are nulled;
  parked secret values are removed. Every tenant-keyed table is cleared, retained on purpose or
  listed. Adapter connection data is operator-owned and listed as skipped, never deleted by the
  API. Projects and repositories on a platform are removed only when the request sets
  `delete_platform_resources`; credentials and trust are always removed.
- **No unaudited gap.** A blocked step holds the deletion and retries with backoff; it advances
  only through an audited `skip-step` by an instance `admin` (who, when, what was left). The
  deletion writes `requested`, one event per step and per skipped category, and `completed`,
  each in the transaction of the step it records, and they survive the tenant's rows. Events
  hold no organisation login or tenant content. The tenant's other audit rows stay until
  `AUDIT_RETENTION_DAYS` ([NFR-0031](NFR-0031-audit-log-is-append-only-complete-and-exportable.md)).
- **Tombstone.** A deleted tenant whose installation still exists is not re-created: its events
  are acknowledged and dropped, and the App must be uninstalled.
- **Isolation.** Other tenants' rows and the host-scoped tables are unchanged, and the engine
  runs only inside the deleted tenant's context ([NFR-0029](NFR-0029-tenant-isolation-by-row-level-security.md)).

## Source

Enterprise baseline row EB-15 (data export and deletion, retention settings; compliance enablers
for SOC 2 / ISO 27001), with EB-4 (audit) and EB-6 (resumable singleton work under leases) of the
plan `docs/plans/2026-10-craftainer-labs-apps.md`; user decisions 1-4 in
[ADR 0034](../adrs/0034-tenant-data-deletion.md) (G-api, 2026-11-03). Stakeholder: Security /
compliance. Operator procedure: [runbook](../runbooks/tenant-deletion.md).

## Verification

Integration tests run as the app role against a real Postgres, in the Checks workflow's prek job
(pytest, 100% coverage gate).

- Whole flow (invariants 1-4, 12, 16): `test_a_deletion_runs_from_the_route_to_the_tombstone_and_defers_then_drops_deliveries`
  and `test_a_skipped_platform_step_goes_on_and_a_skipped_database_step_still_needs_empty_tables`
  in `tests/trustbridge/deletion/test_full_flow_integration.py`;
  `test_the_whole_flow_runs_under_rls_and_every_event_lands_in_the_tenant` in
  `tests/trustbridge/admin/test_tenant_deletion_integration.py`.
- Preconditions, write-ahead, enable refusal, tombstone, last-row removal
  (`tests/trustbridge/tenancy/test_deletion_db_integration.py`):
  `test_a_request_is_refused_for_every_unmet_precondition_and_changes_nothing`,
  `test_a_request_writes_the_record_without_the_login_and_repeats_return_it`,
  `test_enable_is_refused_while_a_deletion_is_open_and_allowed_after`,
  `test_a_tombstone_outlives_its_deletion_record`,
  `test_tb_delete_tenant_refuses_unless_disabled_and_in_the_database_step`,
  `test_tb_delete_tenant_refuses_while_any_tenant_keyed_row_remains`,
  `test_a_delete_and_an_upsert_never_recreate_the_tenant`,
  `test_downgrade_is_refused_while_a_deletion_record_exists_and_round_trips`.
- Engine (idempotent steps, lease, retry): `tests/trustbridge/deletion/test_engine_unit.py`
  (`test_a_deletion_walks_every_step_in_order_and_finishes_with_events`,
  `test_lease_lost_anywhere_stops_without_a_retry_or_a_failure`,
  `test_a_transient_error_retries_with_capped_exponential_backoff_and_counts`);
  `tests/trustbridge/tenancy/test_deletion_engine_db_integration.py`.
- Database step: `tests/trustbridge/deletion/test_database_integration.py`
  (`test_the_step_clears_every_inventoried_table_in_batches_and_resumes_from_its_cursor`,
  `test_other_tenants_rows_survive`, `test_a_holder_without_the_live_lease_deletes_nothing`,
  `test_every_table_with_a_tenant_id_is_cleared_retained_or_listed`).
- Platform and GitHub steps: `tests/trustbridge/deletion/test_platform_unit.py`
  (`test_without_the_flag_projects_and_repositories_are_kept_and_recorded`,
  `test_operator_owned_connection_names_are_recorded_once_at_the_end`),
  `tests/trustbridge/deletion/test_github_unit.py`
  (`test_a_gone_installation_records_repository_ids_and_calls_nothing_else`), and the integration
  files `test_platform_integration.py`, `test_github_integration.py`.
- Admin routes, `skip-step`, `fail`, audit content (invariants 1, 12, 14, 15):
  `tests/trustbridge/admin/test_tenant_deletion_unit.py`
  (`test_only_an_instance_admin_may_change_a_deletion`,
  `test_a_repeat_request_returns_the_open_record_and_audits_it`,
  `test_skip_step_records_who_acknowledged_it_and_audits`,
  `test_a_secret_shaped_reason_is_masked_in_the_event`);
  `tests/trustbridge/deletion/test_audit_unit.py`;
  `test_a_skip_refuses_what_it_must_and_changes_nothing` and
  `test_a_stale_holder_cannot_commit_after_a_skip` in `test_tenant_deletion_integration.py`.
- Purge of finished deletion records (the tombstone is never purged):
  `tests/trustbridge/deletion/test_purge_unit.py`,
  `tests/trustbridge/tenancy/test_deletion_purge_integration.py`.
- Migration: `.github/workflows/migration-compat.yml`; the downgrade and upgrade round trips above.

- Invariant 11 end to end: `test_a_canary_planted_through_a_whole_deletion_is_in_no_log_response_or_row`
  in `tests/trustbridge/deletion/test_canary_flow_integration.py` plants a canary in the credential
  and connection data, in webhook headers and bodies, in the request reason and in a failing step's
  error, runs a whole deletion, and asserts it is in no log record, response, audit row, deletion
  record or delivery row.

Known gaps: the plan names the retry cost of a long tenant
disable as a measurement owed to NG6g, not yet measured; the stale-claim ABA residual of the
delivery requeue (decided for NG6g) has no generation column or test.
