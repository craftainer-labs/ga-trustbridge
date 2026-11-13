# docs/runbooks/

Step-by-step procedures an operator follows when something specific has
happened to a running installation. The first-time and routine operating
pages ([install](../install.md), [upgrade](../upgrade.md),
[backup and restore](../backup-restore.md)) stay one level up; a runbook
lives here when it is an incident or compliance procedure that several of
them point to.

- `tenant-deletion.md` — watching, cancelling and failing a tenant data
  deletion, and replaying completed deletions after a database restore
  ([ADR 0034](../adrs/0034-tenant-data-deletion.md)).

## Do

- Write each runbook as numbered steps an operator can follow without
  reading the code: what to check, the exact command or query, what the
  result means and what to do next.
- Say what a step leaves in the audit log, and what it does not.
- Keep the *why* in the ADR it implements and link to it.

## Don't

- Restate settings tables or chart values: link to the worker and Helm
  READMEs.
