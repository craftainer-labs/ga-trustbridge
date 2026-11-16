# Runbook: tenant data deletion

Operating a tenant data deletion
([ADR 0034](../adrs/0034-tenant-data-deletion.md)): watching it, stopping it,
and putting it right after a database restore. The deletion engine runs in
the reconciler as a singleton duty (`tenant-deletion`, one pass every
`DELETION_POLL_SECONDS`); the settings are in the
[worker README](../../src/trustbridge/worker/README.md) and the chart values
under `trustbridge.deletion` ([Helm README](../../deploy/helm/README.md)).

A deletion walks `requested`, `draining`, `platform`, `github`, `database`,
`done`. `done`, `failed` and `cancelled` are terminal. The tenant stays
admin-disabled the whole time, and for good after `failed` or `cancelled`.

The statements below run as the application role (`trustbridge_app`), which
holds `SELECT` on `tenant_deletions` and `EXECUTE` on the functions named.
The admin API routes wrap them (`POST /admin/v1/tenant-deletions/{id}/cancel`,
`/skip-step` and `/fail`; instance `admin`): prefer them, because they audit
the action (`tenant.deletion.cancelled`, `tenant.deletion.step_skipped`,
`tenant.deletion.operator_failed`). **The SQL functions below write no audit event**, so
if you use one as the break-glass path, record it in your change log.

## 1. Watch a deletion

```sql
SELECT id, tenant_id, state, attempts, retry_at, last_error, counts, skipped, updated_at
FROM tenant_deletions
WHERE state NOT IN ('done', 'failed', 'cancelled')
ORDER BY updated_at;
```

- `attempts` counts failed attempts at the current step. It resets (together
  with `retry_at` and `last_error`) when the step advances and when a save within
  the same step moves the cursor, which is real progress; a save that leaves the
  cursor unchanged leaves it alone. A
  deletion with a rising `attempts` and a `last_error` is blocked: the engine
  keeps retrying with a capped backoff (`DELETION_BACKOFF_BASE_SECONDS`,
  `DELETION_BACKOFF_CAP_SECONDS`, both at least 1) and never advances past a
  blocked step on its own. Alert on `attempts` and on the
  `trustbridge.deletion.attempts` counter. Because a step that flaps between
  failing and advancing (or that fails after each cursor move) keeps `attempts` low, also alert on a non-terminal
  deletion whose `updated_at` is older than your expected step duration.
- `trustbridge.deletion.failed` counts deletions the engine ended as `failed`
  itself (a non-retryable error, for example a step output the audit shapes
  refuse). Each also has a `tenant.deletion.failed` audit event.
- The reconciler's health loop is named `tenant-deletion`. It is stale when it
  has made no progress for its interval plus the longer of `LEASE_TTL_SECONDS`
  and `DELETION_LEASE_TTL_SECONDS`.
- `skipped` lists what was not removed and why (`operator_owned`,
  `installation_gone`, `retention_only`): hand it to the customer or remove it
  yourself.

## 2. Cancel a deletion

A request waits `DELETION_GRACE_SECONDS` (default 86400, 24 hours; Helm
`deletion.graceSeconds`) before the reconciler starts it: `not_before` in the
record shows when. The deletion stays `requested`, and so cancellable, until then.

A deletion can be cancelled only while it is `requested` (nothing destructive
has run: draining is the first destructive step), and a request with a future
`not_before` stays `requested` until then.

```sql
SELECT tb_cancel_tenant_deletion('<deletion id>');
```

It returns `cancelled`, `not_cancellable` (a later state) or `not_found`. The
tenant **stays admin-disabled**; enable it through the admin API when the
cancellation is intended. A new request makes a new record. Prefer the admin
route, which does the same and audits it (`POST
/admin/v1/tenant-deletions/<id>/cancel`, no body; a 409 once past `requested`).
The function itself writes no audit event: if you call it directly, record it
in your change log.

## 2b. Skip a step that cannot complete

A step that keeps failing holds the deletion and retries with backoff (an
alert fires on `attempts`). If you have fixed or accepted what it could not do,
acknowledge it with `POST /admin/v1/tenant-deletions/<id>/skip-step` and
`{"reason": "..."}` (instance `admin`). It is allowed only for the `platform`,
`github` or `database` step, only after the step has failed at least once, and
only while no engine holds the lease (a 409 otherwise: wait for the backoff or
`DELETION_LEASE_TTL_SECONDS`). The record gains a `skipped` entry with the step
and who acknowledged it, and the next step runs; the event is
`tenant.deletion.step_skipped`. Skipping `database` does not remove rows: the
engine goes on to remove the tenant row, which still refuses while any
tenant-keyed row is left, so clear those by hand first.

## 3. Fail a stuck deletion

Use this when a blocked step will not recover and you have decided to stop
(ADR 0034, user decision 2). It ends the deletion as `failed`; nothing is
undone, and what was already removed stays removed. The tenant stays disabled.

1. Check the error and the counts (step 1) and decide what is left to clean up
   by hand.
2. The function refuses (`TB003`) while a live lease is held. A healthy
   engine holds the lease only while a step runs; for a deletion that is
   backing off it is free. If it is held, scale the reconciler to 0 and wait
   `DELETION_LEASE_TTL_SECONDS`.
3. Fail it, with your identity and the reason (both are required; they land in
   `last_error` as `operator <you>: <reason>`):

   ```sql
   SELECT tb_operator_fail_tenant_deletion('<deletion id>', '<your identity>', '<reason>');
   ```

   `TB004` means the record is already terminal, or its `database` step has
   already removed the tenant row, in which case the deletion can only finish.
4. Scale the reconciler back up. To try again, request a new deletion for the
   tenant (the old record is kept).

Prefer the admin route, which does the same and audits it
(`POST /admin/v1/tenant-deletions/<id>/fail` with `{"reason": "..."}`, as an
instance `admin`; it is a 409 while a live lease is held). The function writes
no audit event: if you call it directly, record the action in your change log.

## 4. After a database restore

A restore from a backup returns the database to the backup's moment
([backup and restore](../backup-restore.md)). A deletion that **completed
after the backup** is undone by the restore: the tenant row and its data come
back and the `done` record that blocked re-creation is gone. Deleted data must
not stay restored, so replay those deletions before the system takes traffic.

1. Restore the database but keep the worker and reconciler scaled to 0
   (backup-restore steps 1 to 3). Bring up only the api when you reach step 4
   below.
2. Find the events the restore lost. The audit log is append-only and the
   restore returns it to the backup's `max(seq)`; the SIEM export holds what
   came after (see "The audit log" in backup-restore). From the export take
   every `tenant.deletion.completed` event with `seq` greater than the
   restored `SELECT max(seq) FROM audit_events;`. Its `tenant_id` and `after`
   (the old deletion id and counts) identify the tenant. Without a SIEM, use
   the record of deletion requests you keep in your change log: the response
   to each deletion request names the tenant and the deletion.
3. Check which of those tenants came back:

   ```sql
   SELECT t.id, t.host, t.installation_id, t.org, t.admin_disabled_at
   FROM tenants t WHERE t.id IN ('<tenant id>', '<tenant id>');
   ```

   A tenant that is not listed was deleted before the backup; nothing to do.
   A deletion that was still running at the backup is in `tenant_deletions`
   with its saved state and resumes by itself when the reconciler starts
   (each step is idempotent).
4. For each tenant that came back, with only the api running: disable it
   (`POST /admin/v1/tenants/{id}/disable`, reason "restore replay of
   deletion <old id>"), then request its deletion again
   (`POST /admin/v1/tenants/{id}/deletion`, echoing host, installation id and
   organisation login from the query above, and the same reason). The new
   record gets a new id and its own `tenant.deletion.*` audit events; the
   reason ties it to the original.
5. Scale the worker and reconciler up. The tenants are disabled, so their
   webhook deliveries are deferred, not handled, while the engine runs the
   steps again. Every step is idempotent (ADR 0034), so what was already
   removed before the restore is found gone. If the GitHub App was
   uninstalled in the meantime, the outputs are listed under `skipped` as
   `installation_gone`.
6. Watch the new records finish (step 1) and note the replay in your change
   log, because the audit log cannot be amended.

Backups made before a deletion keep the deleted data until they expire
(ADR 0034, "Backups"): this replay only keeps a *restore* from bringing it
back into service.
