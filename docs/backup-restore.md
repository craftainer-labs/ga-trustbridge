# Backup and restore

Postgres is the only durable state of the application
([ADR 0028](adrs/0028-trustbridge-architecture-and-instance-layout.md)).
Valkey is a stream and a cache that Postgres can rebuild. The chart deploys
neither, so backups are the operator's job on the external services.

## What to back up

| What | Where | Why |
| --- | --- | --- |
| The database | external Postgres | tenants, repositories, webhook deliveries (the outbox), leases, credential operations, schema version and the **audit log** |
| The secret store | the `trustbridge.secretStore.existingClaim` volume, if used | `store:` secret references: parked secret values and tenant connection data. They are not in Postgres |
| Kubernetes Secrets and your values file | your secret manager and Git | database and Valkey credentials, the subscriber secret, the registration state key, signing-key and webhook-secret files |

The GitHub App's private key lives in the configured signer (Vault Transit or
a KMS), so back it up there; trustbridge never holds a copy. Losing it means
registering a new App.

## Postgres

Use the platform's tooling where it has any (managed snapshots, base backups
with WAL archiving for point-in-time recovery). A logical dump also works,
taken as the owner role, which has an `owner_all` policy on every
row-level-security table, so the dump sees every row:

```bash
pg_dump --format=custom --no-owner --file=trustbridge-$(date +%F).dump \
  --host=<primary> --username=trustbridge_owner trustbridge
```

- Dump the primary, not a replica that lags the audit log.
- `--no-owner` is fine because the restore runs as `trustbridge_owner`. Do
  not use `--no-acl`: the dump carries the `GRANT`s the app role depends on.
  Roles themselves are not in a database dump; create them first (see
  [install.md](install.md)).
- Keep backups for no longer than your data-retention promise: deleted
  tenant data persists in backups until they expire
  ([ADR 0034](adrs/0034-tenant-data-deletion.md)). Restoring a single tenant
  is not supported; restore the whole database
  ([ADR 0031](adrs/0031-tenancy-and-row-level-security.md)).

## Restore

1. Stop writers: scale the api, worker and reconciler to 0 (`helm upgrade
   --set api.replicas=0 --set worker.replicas=0 --set reconciler.replicas=0`,
   or `kubectl scale`).
2. Create an empty database and the roles `trustbridge_owner` and
   `trustbridge_app` (no superuser, no `BYPASSRLS`), then restore as the
   owner:

   ```bash
   pg_restore --exit-on-error --no-owner --role=trustbridge_owner \
     --dbname=trustbridge --host=<primary> --username=trustbridge_owner \
     trustbridge-2026-11-13.dump
   ```

   For point-in-time recovery use your platform's restore and recover to
   just before the incident.
3. Check the schema: `SELECT version FROM schema_compat;` returns the version
   the image you are about to run requires, or a lower one. If it is lower,
   `helm upgrade` runs the migrate Job, which brings it forward. Restoring a
   dump taken from a **newer** release into an older image is allowed (the
   code tolerates a newer schema), but restore to the release you backed up.
4. Restore the secret-store volume and the Secrets.
5. Scale the Deployments back up. The processes wait for a current schema,
   then start; the api reports ready when its checks pass.
6. Flush nothing in Valkey: see below. Within `RELAY_RECHECK_SECONDS` (900
   by default) the reconciler re-adds unprocessed deliveries to the stream.

### The audit log

The audit log (`audit_events`, [ADR 0032](adrs/0032-audit-log.md)) is
append-only: the app role can only `INSERT` and `SELECT`, triggers reject
`UPDATE`, `DELETE` and `TRUNCATE` for everyone except the owner role, and
retention deletes only through `tb_audit_purge`.

- A dump and restore keeps every row, its `seq` and the identity sequence's
  position, so a SIEM that resumes the export from its last `since_seq`
  continues without a gap or a repeat. Do not renumber or re-insert rows.
- Restore it with the rest of the database, as the owner. The triggers are
  restored with the table; check they exist afterwards
  (`\d+ audit_events`).
- **Events recorded after the backup are lost** with the rest of the
  database changes. If a SIEM already holds the export up to some `seq`,
  compare it with `SELECT max(seq) FROM audit_events;` after the restore. A
  gap means events between the backup and the failure exist only in the
  SIEM; keep that export, and record the incident and the restore in your
  own change log, because the audit log cannot be amended.
- The export lags by `AUDIT_EXPORT_LAG_SECONDS`, so take backups of the
  SIEM-fed range knowing the newest events are still inside that window.
- Retention (`AUDIT_RETENTION_DAYS`, default 365, `0` keeps everything) runs
  in the reconciler after the restore; a restored old backup can briefly
  contain rows past retention, which the next purge batch removes (and
  records as an `audit.purge` event).

## Valkey: what is lost

Valkey holds the work stream `tb:events` (delivery ids only), its consumer
group, the dead-letter stream `tb:events:dead`, the admin reconcile requests
(`tb:reconcile`) and the rate-limit counters. Nothing in it is the source of
truth, so it needs no backup, but losing it (a flush, a restart without
persistence, a failover that drops data) has these effects:

- **In-flight and queued stream entries are lost.** Every delivery is stored
  in Postgres before GitHub gets its 2xx, and the relay re-adds any row that
  is not processed and not in the stream, on its tick and in a full sweep
  every `RELAY_RECHECK_SECONDS` (900). Deliveries are therefore delayed, not
  lost; handlers are idempotent, so a re-run is safe.
- **The dead-letter stream is lost.** The delivery rows with status `dead`
  stay in Postgres (the stream holds only ids and an error), so they can
  still be listed and requeued through the admin API.
- **Pending admin reconcile requests are lost.** The api answered 202 for
  them; ask again after Valkey is back. (Their consumer is not built yet.)
- **Rate-limit counters reset**, and while Valkey is down the abuse limiter
  fails open and the webhook route still stores deliveries (ADR 0028).

With Valkey persistence (`appendonly yes`) a restart keeps the stream; you
still do not need to back it up.

## Test the restore

Restore a recent dump into a scratch database and start a copy of the chart
against it (a separate namespace and a different `publicBaseUrl` that GitHub
never calls) at least once per quarter, and after every major upgrade. Check
that `audit_events` has the expected `max(seq)` and that the api becomes ready.
