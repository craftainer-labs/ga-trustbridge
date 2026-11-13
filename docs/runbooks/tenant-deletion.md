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
The admin API routes that wrap cancel and operator-fail arrive with the
request endpoint (NG6h-7); the functions are the break-glass path after that.

**Cancel and operator-fail are unaudited until NG6h-7's admin routes exist.**
The SQL functions below write no audit event, so record each use in your
change log. Once the routes exist, prefer them: they audit the action, and the
functions stay as the break-glass path.

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

A deletion can be cancelled only while it is `requested` (nothing destructive
has run: draining is the first destructive step), and a request with a future
`not_before` stays `requested` until then.

```sql
SELECT tb_cancel_tenant_deletion('<deletion id>');
```

It returns `cancelled`, `not_cancellable` (a later state) or `not_found`. The
tenant **stays admin-disabled**; enable it through the admin API when the
cancellation is intended. A new request makes a new record. Cancelling writes
no audit event itself: record it in your change log until the admin route
audits it.

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

The function writes no audit event: record the action in your change log until
the admin route audits the acknowledgement.

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
4. For each tenant that came back, with only the api running (until NG6h-7
   adds the request route, run the SQL functions instead:
   `tb_set_tenant_disabled` and `tb_request_tenant_deletion(tenant, host,
   installation id, org, by_sub, by_iss, delete_platform, not_before)` as the
   application role, and record the actions in your change log): disable it
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
