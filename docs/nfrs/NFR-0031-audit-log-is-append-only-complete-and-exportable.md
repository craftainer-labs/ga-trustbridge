# NFR-0031. The audit log is append-only, loses no terminal event, and is exportable with bounded retention

## Status

Accepted. Implemented; the SIEM push is a follow-up and is not part of this requirement.

## Attribute

Security (accountability, non-repudiation), compliance (SOC 2 / ISO 27001 enablers), reliability.

## Description

- **Append-only in the database.** As the app role, `UPDATE`, `DELETE` and `TRUNCATE` on
  `audit_events` fail; a trigger rejects them even if a grant is mistakenly widened, and only the
  owner role passes. The only delete path is `tb_audit_purge`. `occurred_at` and `seq` are
  assigned by the database. An unauthenticated rejection produces a log line and a metric, not a
  row.
- **Content.** Every mutating admin endpoint writes exactly one event (who by `sub` and `iss`,
  what, when, source address, `before` and `after`) in the same transaction as its change, so a
  change that rolls back leaves no `success` event. `before` and `after` hold only allow-listed
  fields: no secret, email, login or display name appears in any column.
- **No silent loss of a terminal event.** The reconciler's terminal event for a request
  (`tenant.reconcile.completed` or `tenant.reconcile.failed`) is delivered at least once: it is
  written before the request is acknowledged, keyed by a per-request id, and the write is
  idempotent on `(tenant_id, action, event_key)` (`ON CONFLICT DO NOTHING`), so a reclaim or a
  retry after a crash yields exactly one row. `event_key` is internal and never exported. An
  audit-store outage leaves the request in flight for a retry and does not use up its attempts.
  Throttled `authz.denied` events never lose a count: a summary row records what was suppressed
  (duplicates are allowed and recognisable by `window`, loss is not).
- **Scope and export.** A tenant-scoped `auditor` reads only its tenant's events; an
  instance-scoped `auditor` reads all, including events with no tenant. Export is NDJSON,
  ordered by `seq`, resumable from the last `seq` with no gap or repeat, returns nothing newer
  than `AUDIT_EXPORT_LAG_SECONDS` (default 30), and itself writes an event naming the reader and
  the range. No route writes, updates or deletes an event.
- **Retention.** `AUDIT_RETENTION_DAYS` (default 365; `0` never purges); a non-zero value below
  `AUDIT_RETENTION_MIN_DAYS` (default 90) is refused at startup and by the database function.
  The reconciler purges in bounded batches as a singleton duty. The app-computed cut-off is moved
  back by `CLOCK_SKEW_MARGIN` (5 minutes) so a retention equal to the floor survives an app
  clock ahead of the database.

## Source

Enterprise baseline rows EB-4 (append-only audit log, JSON export for a SIEM), EB-15 (retention
settings) and EB-6 (a singleton purge duty; Postgres as the only durable state) of the plan
`docs/plans/2026-10-craftainer-labs-apps.md`. Stakeholder: Security / compliance. Design:
[ADR 0032](../adrs/0032-audit-log.md). At-least-once with idempotent writes follows the OWASP
Logging Cheat Sheet and the standard outbox-consumer pattern (plan Decision log, 2026-11-16,
NG5r-d). Builds on [NFR-0027](NFR-0027-expand-contract-migrations.md) for the migrations.

## Verification

Integration tests run as the app role against a real Postgres in the Checks workflow's prek job
(pytest, 100% coverage gate). The audit migrations are also covered by the Migration compat job
(`.github/workflows/migration-compat.yml`).

- Append-only, content and clock (ADR 0032 invariants 1-4, 3a, 15):
  `tests/trustbridge/audit/test_audit_integration.py`:
  `test_the_app_role_can_insert_and_select_but_not_update_delete_or_truncate`,
  `test_the_trigger_rejects_changes_by_anyone_but_the_owner_role`,
  `test_the_table_is_forced_row_level_security_and_the_functions_are_hardened`,
  `test_a_null_tenant_event_is_inserted_without_a_context_and_foreign_ones_are_not`,
  `test_the_database_assigns_the_timestamps_and_seq`,
  `test_the_purge_function_is_the_only_delete_path`,
  `test_a_retention_at_the_floor_survives_an_app_clock_ahead_of_the_database`,
  `test_retention_zero_never_purges_and_otherwise_purges_in_batches`,
  `test_the_reconciler_purge_job_removes_old_rows_in_bounded_runs`.
- `CLOCK_SKEW_MARGIN`: `test_purge_expired_moves_the_cutoff_back_by_the_clock_skew_margin` in
  `tests/trustbridge/audit/test_audit_unit.py`.
- Redaction (invariant 6): `test_a_canary_in_a_credential_name_reference_or_error_reaches_no_row`
  (integration); `tests/trustbridge/audit/test_redact_shapes_unit.py`, `test_credentials_unit.py`,
  `test_validation_unit.py`.
- Same-transaction emission (invariants 8, 9):
  `test_a_credential_event_commits_with_its_change_and_not_without_it`;
  `test_the_disable_and_enable_routes_write_the_columns_and_the_event_together` in
  `tests/trustbridge/admin/test_reads_integration.py`;
  `test_every_deletion_route_has_its_role_bucket_and_emitter` in
  `tests/trustbridge/admin/test_tenant_deletion_unit.py`; the mutation walk
  `test_the_mutation_walk_fails_for_a_route_without_an_emitter_or_the_mutation_bucket` in
  `tests/trustbridge/admin/test_guards_unit.py`.
- At-least-once terminal events and `event_key` (NG5r-d):
  `test_a_second_write_with_the_same_event_key_stores_one_row`,
  `test_the_event_key_is_scoped_to_the_tenant_and_the_action`,
  `test_the_event_key_is_never_exported_or_read_back`,
  `test_the_reconcile_audit_writes_one_event_per_request_id`,
  `test_the_event_key_migration_round_trips` (integration file above);
  `tests/trustbridge/worker/test_reconcile_queue.py`:
  `test_a_crash_between_the_write_and_the_ack_writes_the_same_key_again`,
  `test_an_audit_outage_does_not_use_up_the_attempts`,
  `test_an_event_that_cannot_be_written_leaves_the_request_inflight_and_retried`,
  `test_without_an_audit_store_the_request_is_acked_with_a_warning`;
  `tests/trustbridge/worker/test_reconcile_queue_integration.py`:
  `test_the_request_id_is_kept_across_a_reclaim_and_removed_by_the_ack`;
  `test_the_reconcile_duty_writes_its_terminal_events_in_the_requests_tenant` (audit integration).
- Webhook-delivery idempotency: `test_a_redelivered_webhook_event_writes_one_row` and
  `test_events_without_a_delivery_id_are_never_deduplicated`.
- Denial throttle without loss: `tests/trustbridge/audit/test_throttle_unit.py`
  (`test_a_restarted_process_finishes_the_work_from_the_shared_store`,
  `test_a_denial_that_cannot_be_noted_for_a_summary_is_recorded_itself`).
- Export-lag bound (a transaction longer than the lag is aborted):
  `test_a_transaction_that_outlives_the_timeout_is_aborted` and
  `test_the_check_fails_when_the_lag_is_not_above_the_bound` in
  `tests/trustbridge/tenancy/test_timeout_check_integration.py`.
- Scope and export (invariants 11-14): `test_a_tenant_reads_only_its_own_events_and_instance_scope_reads_all`,
  `test_the_cursor_is_ordered_resumable_and_filters_agree_across_scopes`,
  `test_the_export_is_ndjson_ordered_by_seq_and_resumable`,
  `test_the_export_stops_before_the_first_event_inside_the_lag` (audit integration);
  `test_the_export_is_ordered_paged_scoped_and_audited` and `test_no_audit_route_changes_a_row` in
  `tests/trustbridge/admin/test_admin_integration.py`; `tests/trustbridge/audit/test_export_event_unit.py`;
  `test_no_audit_route_writes_or_deletes` and `test_only_an_auditor_reads_audit` in
  `tests/trustbridge/admin/test_routes_unit.py`.

Known gaps (named in the plan as measurements owed to NG6g, not yet done): the flush cost of the
denial throttle's pending-base set is documented in `src/trustbridge/audit/README.md` as to be
measured, with no benchmark or test; a never-accepting audit store makes the convergent
reconcile body re-run every claim TTL with an ERROR log each time (accepted residual, no test of
the alert). The SIEM push and a DBA-level tamper (outside the threat model) are not covered.
