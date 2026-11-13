# Upgrading trustbridge

`helm upgrade` runs the migrate Job (a pre-upgrade hook) before the
Deployments change. Migrations follow expand/contract
([NFR-0027](nfrs/NFR-0027-expand-contract-migrations.md)) and are immutable
once applied ([NFR-0019](nfrs/NFR-0019-migrations-immutable-once-applied.md)),
so the previous release keeps working against the migrated schema while the
rollout replaces pods.

## Steps

1. **Back up first** ([backup-restore.md](backup-restore.md)): a Postgres
   dump, and the secret-store volume if you use one.
2. Read the release notes for the new chart and image version, and compare
   your values with the new `values.yaml` (`helm show values`). New settings
   have defaults, so an upgrade needs no new value unless the notes say so.
3. Upgrade:

   ```bash
   helm upgrade trustbridge deploy/helm/trustbridge -n trustbridge -f my-values.yaml
   ```

   Pin the image by digest in `image.digest` (the digest of the signed image
   index) so the upgrade is exactly what you reviewed.
4. Watch the hook Job and the rollout:

   ```bash
   kubectl -n trustbridge get jobs,pods
   kubectl -n trustbridge rollout status deploy/trustbridge-api
   ```

## PostgreSQL version and the transaction timeout

The audit export's lag guarantee needs every transaction of the api, worker and reconciler to end
within `AUDIT_EXPORT_LAG_SECONDS` ([ADR 0032](adrs/0032-audit-log.md)). The app sets
`SET LOCAL transaction_timeout` from `trustbridge.audit.transactionTimeoutSeconds` (default 15,
which must be below `exportLagSeconds`, default 30) and checks at start-up that the database
applies it: in production a failing check keeps the pod not ready and then exits after
`SCHEMA_WAIT_SECONDS`.

`transaction_timeout` exists from **PostgreSQL 17**, which production requires: on an older server
the check fails closed (not ready, then exit after `SCHEMA_WAIT_SECONDS`). Outside production the
app sets `statement_timeout` and `idle_in_transaction_session_timeout` to the same value instead
and logs a warning: each statement and each idle gap is bounded, but a transaction of many short
statements is not, so the lag guarantee is weaker. A stricter value the role or database already sets (`ALTER ROLE ... SET transaction_timeout`) is never loosened: the app applies the smaller of that and the configured one. The
migrate Job is not bounded (it runs as the owner role on its own connection).

## If the migration fails

The Job runs once with `database.migrate.backoffLimit` retries and a deadline
(`activeDeadlineSeconds`, 900 s). A failure fails `helm upgrade` and the old
Deployments keep running on the old schema. Read the Job's logs
(`kubectl logs job/trustbridge-migrate`); the Job takes a Postgres advisory
lock, so a second run waits instead of interleaving. It refuses to run as any
role except `database.migrate.expectedRole`. The Job is deleted when it
succeeds and before the next hook runs; a failed one stays for inspection.

## Rolling back

`helm rollback` returns the Deployments to the previous image. The schema is
not rolled back: the previous release stays compatible with the newer schema
by the expand/contract rule (processes wait for a schema at least as new as
the one they require, and tolerate a newer one). A destructive "contract"
migration ships only in a later release than the code that stopped needing
the old shape, so roll back one release at a time. If a migration itself must
be undone, restore the database from the backup taken in step 1.

## Row-level security rollout

Tenancy and RLS use a two-release N-1 rollout
([ADR 0031](adrs/0031-tenancy-and-row-level-security.md), NFR-0027): do not
skip releases between versions that change the same table.
