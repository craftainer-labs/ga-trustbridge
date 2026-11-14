# 0034. Tenant data deletion (EB-15): a write-ahead, resumable operation

## Status

Proposed. The user decided on 2026-11-03 (G-api) that tenant data deletion is **in NG6**,
overriding the earlier lean to defer it; this ADR holds its design and the user's answers on
the remaining items ("User decisions"). No code is written yet. It becomes Accepted when the
implementation merges. Builds on
[0028](0028-trustbridge-architecture-and-instance-layout.md) (write-ahead operations, leases,
parked values, retention),
[0029](0029-adapter-plugin-interface.md) (adapter `delete` operations),
[0031](0031-tenancy-and-row-level-security.md) (tenant context, definer functions),
[0032](0032-audit-log.md) (the audit trail) and
[0033](0033-admin-api-v1-rbac-and-limits.md) (the endpoint, roles, tenant disable).

## Context

EB-15 asks for compliance enablers for SOC 2 and ISO 27001: encryption in transit, data export
and deletion, retention settings. Retention is covered (webhook bodies in 0028, audit in 0032).
Deletion means removing everything trustbridge holds for one tenant, and what it created
elsewhere on the tenant's behalf. What exists for a tenant:

1. **Database rows.** `tenants`, `tenant_repositories`, `scope_leases`, `adapter_resources`,
   `adapter_credentials`, `credential_operations` (all tenant-keyed, under RLS, 0031), plus
   `audit_events` rows (0032) and `webhook_deliveries` rows, which are host-scoped and carry no
   tenant id (their bodies contain the tenant's payloads).
2. **Platform-side state** created through adapters (0029): projects, robot accounts, access
   tokens, OIDC identity mappings, webhook policies, repository credentials. Some of it holds
   customer data (a Harbor project holds images).
3. **GitHub-side outputs** written with the installation token: secrets, variables and
   environments in the tenant's repositories (0028 invariant 21 keeps them recoverable).
4. **Secret-store entries**: parked secret values (removed on terminal operations, 0028
   invariants 23), the per-tenant Argo CD bearer token, and the adapter **connection data**
   `store:tenants/<uuid>/<adapter>`, which is operator-provisioned and which the admin API never
   writes (0033, decided).
5. **The installation may still exist.** An admin can delete data while the GitHub App is still
   installed; the next installation event would then create a new tenant with a new id.
6. **The operation outlives its tenant.** Once the `tenants` row is deleted the record of the
   deletion cannot live in tenant-keyed, RLS-covered tables.

## Decision

### The operation

Deletion is an operation like a credential operation (0028): a **write-ahead record** is
committed before any destructive step, each step is idempotent and recorded, and the
reconciler resumes a non-terminal deletion.

- **Table `tenant_deletions`**, host-scoped and outside RLS like `webhook_deliveries`
  (0031 "Already decided"), with no foreign key to `tenants`: `id` (UUID), `tenant_id`,
  `host`, `installation_id`, `requested_by_sub` and `requested_by_iss`, `requested_at`,
  `delete_platform_resources` (the request flag, user decision 1), `state` (`requested`, `draining`, `platform`, `github`, `database`, `done`, `failed`, `cancelled`; the last three are terminal; NG6h-1 added `cancelled`, the flag column and the nullable `not_before`),
  `step_cursor` (jsonb: the position inside the current step), `counts` (rows and resources
  removed per category), `skipped` (jsonb: what was not removed and why), its own lease
  (`holder`, `fence`, `expires_at`, same fencing rule as 0028 invariant 10), `updated_at`,
  `last_error`. It stores no organisation name or other tenant content, only ids and counts.
- **Endpoint.** `POST /admin/v1/tenants/{t}/deletion` (0033: `admin`, instance scope). It is
  **idempotent per tenant**: while a deletion for the tenant is non-terminal, the same
  request returns the existing record. `GET /admin/v1/tenant-deletions` and
  `GET /admin/v1/tenant-deletions/{id}` read the record (a tenant-keyed path would 404 once the
  tenant row is gone). The response to `POST` is 202 with the record.

### Preconditions

A request is rejected (409 Problem Details) unless all hold:

1. The tenant is **admin-disabled** (0033), so no handler runs and no new adapter operation
   starts other than this deletion's own delete and revoke operations (the one exception to the
   disable gate, in 0031 and 0033).
2. The request body **echoes the tenant's host, installation id and organisation login**
   (a typed confirmation of an irreversible action), which is checked and **not stored**, and
   carries a reason (recorded in the audit).
3. No other deletion for the tenant is non-terminal (a duplicate returns it instead).
4. The caller has `admin` at instance scope. A tenant-scoped role cannot delete.

A tenant with a non-terminal deletion cannot be enabled (0033 invariant 19).

### Steps (each idempotent, in this order)

1. **Draining.** Abandon in-flight `credential_operations` of the tenant under the normal
   rules (their parked values are removed, 0028 invariant 23); wait until no lease is held for
   the tenant or its TTL passes. Handlers already skip a disabled tenant.
2. **Platform.** For each tracked `adapter_resources` and `adapter_credentials` row, run the
   adapter's delete or revoke operation through the normal write-ahead `credential_operations`
   path (so a crash is resumed, and the platform-side name is deterministic, 0029). **What is
   deleted is user decision 1.**
   *Implemented (NG6h-5, `deletion/platform.py`, `worker/platform_deletion.py`).* One pass removes
   one recorded item, in this order: credentials (`CredentialCore.delete_scope` with the deletion's
   `deletion_id` and `remove_outputs=False`), then webhooks, repositories, projects
   (`ResourceCore.delete`). Resources are **not write-ahead** (only credentials are, through
   `credential_operations`): a crash between the adapter call and forgetting the row repeats the
   adapter's idempotent `delete`. The work list is re-read from the rows on every pass, so a restart
   converges; a removed item is not removed twice, and after a crash `counts` is a lower bound by
   at most one. Repositories and projects stay, and are recorded under `skipped` as
   `kept_by_request`, unless the request set `delete_platform_resources`. The GitHub outputs of a
   deleted credential stay recorded for step 3 (`remove_outputs=False`): step 2 makes no GitHub call,
   so an uninstalled App cannot block it. Nothing is read from the platform: only recorded rows are
   removed. A failure (the platform down, an adapter not enabled or without connection data, a
   delete waiting for a manual action, an unrecognised resource kind) is transient: the engine
   records it, backs off, keeps the deletion in `platform` and raises the attempts alert; only an
   operator's `skip-step` moves on (user decision 2). Counts are `platform_access_grants`,
   `platform_webhooks`, `platform_repositories` and `platform_projects`.
3. **GitHub.** Remove the secrets, variables and environments trustbridge wrote, as tracked in
   the tenant's resource and credential rows, using an installation token, per repository. If
   the installation no longer exists (the App was uninstalled), GitHub has already revoked
   access and the outputs cannot be removed from our side: they are recorded in `skipped` as
   `installation_gone`, with repository ids, so the customer can remove them. Nothing outside
   the tracked outputs is touched.
4. **Secret store.** Remove the tenant's parked values (the drain abandons them). *Settled
   (NG6h-5):* the per-tenant Argo CD token is part of the operator-provisioned connection data and is
   **not** deleted; there is no separate secret-store state, and the platform step records the
   connection data as skipped (below).
   **The adapter connection data (`store:tenants/<uuid>/<adapter>`) is not deleted by the API**,
   consistent with it being operator-only (0033): the record lists the keys under `skipped` as
   `operator_owned`, for the operator to remove. *Implemented (NG6h-5):* the platform step's last
   pass lists `store:tenants/<uuid>/` (names only, `ConnectionNames`) and records one
   `operator_owned_connections` / `operator_owned` skipped entry per adapter name, never a value
   or a reference. (User decision 4: it keeps the API from gaining any write path to that store.)
5. **Database.** In the tenant's context (0031), in batches and in the order
   `credential_operations`, `adapter_credentials`, `adapter_resources`, `scope_leases`,
   `tenant_repositories`, then the `tenants` row last through `tb_delete_tenant` (0031; the app
   role has no `DELETE` on `tenants`), each batch its own transaction (bounded by the
   `transaction_timeout` the `after_begin` hook sets, B8, so a batch must be sized to finish inside
   `TRANSACTION_TIMEOUT_SECONDS`, below the audit export lag) and
   counted (NG6h-4b: the `DatabaseStep` runs as the app role in the tenant's context, where the
   `tenant_isolation` policies admit `DELETE`, so it needs no definer function; it removes everything above except the `tenants`
   row, which the engine deletes in its finish transaction; counts are named `adapter_operations`, `access_grants`,
   `adapter_resources`, `scope_leases`, `tenant_repositories` and `webhook_bodies`, because the table names `credential_*`
   hold a secret word the audit view would hide). Webhook bodies are cleared for deliveries recorded for the tenant: an expand
   migration adds a nullable `tenant_id` to `webhook_deliveries`, set by the worker when it
   resolves the delivery (NG6h-2: before it defers or handles it; an event with no registered
   handler, or a payload naming no installation, is not resolved); deletion nulls `body`, `headers` (to `{}`) and `last_error` and sets the row terminal with status `deleted`, which the relay, the
   re-relay sweep and the worker skip (the row stays for deduplication, as the retention purge
   keeps rows, 0028 item 5), so a held, body-less row is never re-relayed. Rows with no `tenant_id` (never resolved, or older than
   the column) fall to the normal retention and are counted under `skipped` as
   `retention_only`: the last pass counts the deliveries on the deletion's host that have no `tenant_id` and still hold a body
   (`webhook_bodies_unresolved`), a host-wide upper bound because such a row cannot be attributed to the tenant.
6. **Done.** The record's `state` becomes `done` with its counts. The audit trail of the
   deletion is written throughout (below).

A step that fails records the error and is retried with backoff; a **blocked step** (for example
a platform that stays unreachable) holds the deletion in that state until the operator decides
(user decision 2). The reconciler resumes non-terminal deletions whose lease is free, as a
singleton duty (EB-6). There is no cancel after the first destructive step; before it (state
`requested`) an admin may cancel.

### The audit trail of the deletion

Events (0032) are written by the deletion itself: `tenant.deletion.requested` (who, reason, and the tenant id,
host and installation id; **no organisation login and no tenant content**, `before` is ids,
states and counts only, so no organisation name or other tenant content survives in the append-only trail (only the
opaque identifiers, see Open issues)), one `tenant.deletion.step`
per transition (including `requested` to `draining`, the first destructive step) with cumulative counts, `tenant.deletion.skipped` per skipped category, and
`tenant.deletion.completed`. They carry the tenant id, are inserted in the same transaction as
the step they record, and **survive the deletion**: `audit_events` has no foreign key to the
tenant and the database step never touches it. The reconciler's audit purge (0032) removes the events
only at retention. What happens to the **other** audit rows of the tenant is user decision 3.

### After deletion

The tenant id no longer resolves. If the GitHub installation still exists (the App was not
uninstalled), its events are **acknowledged and dropped**: a row in `tenant_tombstones` for
`(host, installation_id)` is a tombstone, so `tb_upsert_tenant` and `SqlTenants` create nothing,
and the worker marks the delivery processed without running a handler (user decision B2, NG6h-2).
The only answer to the installation's admins is the response to the deletion request and its
record, which say the App must be uninstalled; a reinstall gets a new installation id and works.
The worker has no tenant to answer through (no row, no installation token mapping), so it logs the
drop (host, delivery id and event only) and sends nothing to GitHub. While a deletion is in
progress the tenant is admin-disabled, so its deliveries are deferred and left pending as for any
disabled tenant (ADR 0033 invariant 19) until the deletion removes them; the lifecycle and
repository handlers also refuse a deleting, `deleted` or tombstoned tenant (nothing is written or
scheduled), and `set_installation_state` is refused for it under the same advisory lock.

## User decisions (G-api, 2026-11-03)

1. **Platform step: a per-request flag.** **User decision (G-api, 2026-11-03).** The request
   carries `delete_platform_resources`, default **false**. Credentials and trust (tokens, robot
   accounts, OIDC identity mappings, webhook policies, repository credentials) are always
   removed; projects and repositories, which hold customer artifacts, are removed only when the
   flag is set, and the confirmation then echoes it. The record lists per adapter what was
   removed. (Considered and not chosen: credentials only with no flag; everything always.)
2. **A step that cannot complete: block and retry, with an audited `skip-step`.** **User
   decision (G-api, 2026-11-03).** The deletion stays in the blocked step and retries with
   backoff (an alert is raised); it never advances on its own. An instance-scoped `admin` may
   call `POST /admin/v1/tenant-deletions/{id}/skip-step`, which records in `skipped` what was
   left behind, who acknowledged it (`acknowledged_by`) and when, is audited, and lets the next
   step run. (Considered and not chosen: an automatic skip after a timeout.)
3. **The deleted tenant's other audit rows: kept until normal retention.** **User decision
   (G-api, 2026-11-03).** Audit rows of the tenant stay until `AUDIT_RETENTION_DAYS`, so the
   append-only guarantee stays whole; `before` and `after` are redacted allow-lists (0032) and
   hold few tenant data. (Considered and not chosen: stripping them through an owner-only
   function, or purging them at deletion.)
4. **Connection data: not deleted by the API.** **User decision (G-api, 2026-11-03)**
   (accepted judgement call). Adapter connection data `store:tenants/<uuid>/<adapter>` is
   operator-owned, matching the operator-only rule (0033); deletion lists its keys under
   `skipped` as `operator_owned` for the operator to remove.

Sources: EB-15; 0029 adapter delete semantics; 0028 invariants 22-23; GDPR Art. 17(3)(b) and
(e), as general context and not legal advice.

## Decision needed

None open.

## Invariants the implementation must test

Each is one testable sentence; the implementing item tests them against a real Postgres and
adapter fakes.

**Preconditions and idempotency**

1. A deletion request for a tenant that is not admin-disabled, whose confirmation does not
   echo its host, installation id and organisation login, or whose caller is not an
   instance-scoped `admin`, is rejected and changes nothing.
2. A second request for a tenant with a non-terminal deletion returns the same record and
   starts no second one, and `enable` for such a tenant is rejected.
3. The deletion record is committed (write-ahead) before any destructive step, and survives
   the deletion of the `tenants` row (no foreign key, outside RLS).
4. Every step is idempotent: killing the process at any point between two statements and
   running the reconciler again ends in `done` with the same final state and counts, and a
   step already completed is not repeated destructively.
5. Two reconcilers never run the same deletion at once (lease and fence, 0028 invariants 9
   and 10); a holder whose lease expired cannot commit a step.

**What is deleted and what is not**

6. After `done`, no row with the tenant's id exists in the six tenant-keyed tables, and the
   `tenants` row is the last one removed.
7. After `done`, the bodies of the webhook deliveries recorded for the tenant are null and their status is `deleted` (never relayed or handled again), and a
   delivery with no recorded tenant is left to normal retention and listed under `skipped`.
8. The platform step calls only adapter delete or revoke operations through the write-ahead
   path; with `delete_platform_resources` unset it deletes no project or repository (user decision 1), and with it set it deletes exactly what
   the tenant's resource rows track.
9. The GitHub step removes only the secrets, variables and environments tracked for the
   tenant; for an uninstalled App it removes nothing, makes no GitHub call that needs the
   token, and records `installation_gone`.
10. The admin API writes no connection data and the deletion deletes none: the keys of
    `store:tenants/<uuid>/<adapter>` are listed under `skipped` as `operator_owned`.
11. Parked secret values of the tenant are removed (0028 invariant 23), and a canary secret is
    in no row, log line or audit event of the deletion.

**Audit and isolation**

12. The deletion writes `requested`, one event per completed step, one per skipped category
    and `completed`, each in the same transaction as the step it records, and all of them
    exist after the tenant's rows are gone.
13. `audit_events` rows of other tenants and the host-scoped tables are unchanged by a
    deletion, and the deletion of tenant A never runs with another tenant's context (0031
    invariants 1, 2, 11).
14. No `tenant.deletion.*` event or `tenant_deletions` column contains the organisation login
    or other tenant content, and the confirmation is verified without being stored; the audit
    purge removes the deletion's events only after retention, and the append-only
    triggers still reject updates and deletes of those rows for the app role.

**Failure policy**

15. A blocked step holds the deletion in that state and never advances to `database` without
    an acknowledgement (user decision 2); the acknowledgement is audited with
    the acknowledger's `sub` and what was left.
16. A tenant whose installation still exists is not re-created: its events are acknowledged and
    dropped (tombstone), and the deletion record and response say the App must be uninstalled.
17. The migration (the `tenant_deletions` table, the `webhook_deliveries.tenant_id` column)
    passes the `migration-compat` job and the previous release's suite (NFR-0027).

## Open issues

- **Engine transitions (NG6h-4a-1, migration `5d7a2c8e1b94`).** The record is written only by `SECURITY DEFINER`
  functions (claim with a fence bump and `SKIP LOCKED`, renew, release, retry with `attempts` and `retry_at`, advance,
  fail, cancel, finish), each guarded by the live lease (`TB003`) and a valid transition (`TB004`); `tb_delete_tenant`
  checks holder and fence. `failed` is written only for an explicit non-retryable error (with the lease) or by an operator
  (`tb_operator_fail_tenant_deletion`, refused while a live lease is held); transient
  errors retry with capped backoff. The engine package (`src/trustbridge/deletion/`, NG6h-4a-2) is built: each definer transition and its
  audit events are one session transaction (the functions write no `audit_events`), a run ends after about half
  the lease TTL, `TB003` and `TB004` both stop the run, the `tenants` row is removed by the engine's finish
  transaction (after the `DatabaseStep`'s batches), and a step that fails is retried with capped backoff while
  `failed` needs a `NonRetryableError`. A `ValueError` while building the audit events of a step's output (a bad
  category name or count) is a programming error and ends as `failed` (code `invalid_step_output`), not a retry loop.
- **Hardening (NG6h-4a follow-ups, migration `6f3b9d2e4a85`).** Expand-only, no signature changes. (1) Every older
  definer gets `pg_temp` last in its `search_path` (ADR 0031 invariant 10). (2) `tb_advance_tenant_deletion`: a
  same-state advance whose step cursor *changes* is real progress and also resets `attempts`, `retry_at` and
  `last_error`, as a step change does; a same-state save with an unchanged (or no) cursor still leaves them alone, so a
  poison step keeps counting. (3) `tb_delete_tenant` also refuses (55000, `webhook_deliveries` in the list of
  non-empty tables) while a `webhook_deliveries` row of the tenant has a `status` other than `deleted`, before the delete
  and in the re-check after it: an erasure backstop for the `DatabaseStep`. The lock order is unchanged (advisory lock,
  deletion row, `tenants` row). The downgrade refuses (55000) while any non-terminal deletion exists and restores the
  `5d7a2c8e1b94` bodies and paths.
- **Database step (NG6h-4b).** `deletion/database.py`, `SqlDatabaseStep`: one bounded batch per pass in one transaction in
  the tenant's context, with a lease guard (holder, fence, unexpired, state `database`) as its first statement, then the
  tables in the order above and the delivery bodies. Inventory gaps found, none changed here: (1) the `tenant_id` of a
  delivery is set only when the worker resolves it, so an event with no registered handler that names the tenant's
  installation keeps its body until retention and appears only as the host-wide `retention_only` count; (2) a cleared
  delivery keeps only its dedupe key (`host`, `delivery_id`, `body_sha256`), `event`, timestamps and counters: `body`,
  `headers` (NOT NULL, so `{}`) and `last_error` (handler error text, may name tenant data) are cleared, since erasure
  covers every field that can hold personal or tenant data; (3) the inventory has no tenant-keyed table beyond the six,
  and the integration test fails if one is added without a decision.
- **Batches by primary key (NG6h-4c).** A batch is keyed on the table's primary key (`WHERE pk IN (SELECT pk ... ORDER BY pk LIMIT n FOR UPDATE SKIP LOCKED)`), not on `ctid`, so a concurrent update cannot move a row out of it (NG6h-4c); a short batch that leaves a row of the tenant (one another transaction held locked) keeps the cursor on the table, so the next pass takes it, and `tb_delete_tenant`'s emptiness check is the backstop. `FOR UPDATE` needs `UPDATE` privilege and, under RLS, an `UPDATE` policy on each batched table; the app role has both (`SELECT, INSERT, UPDATE, DELETE` grants; `tenant_isolation` is `FOR ALL`), and the integration test exercises it as `trustbridge_app` (no `BYPASSRLS`) with RLS forced on the scratch database it provisions (the migrations still create RLS inert).
- **Wiring (NG6h-4a-3).** The reconciler runs the engine as a singleton duty (`tenant-deletion`, under a lease like
  the audit purge) through the `DeletionDriver` Protocol only: at most `DELETION_BATCH_SIZE` claims per poll of
  `DELETION_POLL_SECONDS`, a liveness beat per pass, and a store error that escapes `run_once` is logged and ends that
  poll, so the deletion's own lease lapses and it is claimed again. Poll, batch, lease TTL, budget fraction, backoff
  base and cap, drain passes and drain poll are settings with Helm values (`trustbridge.deletion.*`); at startup the drain
  poll plus the configured step pass time must fit in half the deletion lease TTL (an error in production, a warning
  elsewhere). The gate (B10): `SqlTenantGate.allows`, the adapter core's `OperationGate`, admits a `delete` operation
  through a disabled tenant only when it carries the id of a non-terminal deletion of that same tenant (checked in the
  gate's one statement; `CredentialCore.delete_scope(..., deletion_id=)`, which only the deletion engine's steps set).
  A user-initiated delete on a deleting tenant, every other kind, a refresh and an orphan sweep stay refused. Composing the engine with its steps is left to the items that build them (4b, 5, 6).
  The runbook (cancel, operator-fail, and replaying `tenant.deletion.completed` after a restore) is
  [`docs/runbooks/tenant-deletion.md`](../runbooks/tenant-deletion.md).
- **Re-creation (decided, NG6h-1; the table moved in NG6h-3c-1).** A row in `tenant_tombstones` for
  `(host, installation_id)` (until NG6h-3c-1 a `done` row in `tenant_deletions`; or a
  `database`-step deletion row whose tenant row `tb_delete_tenant` already removed) is a tombstone: `tb_upsert_tenant` creates nothing (NG6h-1 returned NULL; NG6h-2 raises, below), and `SqlTenants` and
  `MemoryTenants` raise `TenantDeletedError`. A reinstall gets a new installation id and is not
  blocked; an open, failed or cancelled deletion is not a tombstone. The ports also refuse a
  `deleted` tenant and one with a non-terminal deletion, and the installation handler drops the
  event (NG6h-1). `tb_upsert_tenant`, `tb_delete_tenant`, `tb_request_tenant_deletion`,
  `SqlTenants.upsert_installation` and `SqlTenants.set_installation_state` take one
  per-installation advisory lock first, so a concurrent delete and upsert or state change cannot
  recreate or rewrite the tenant. From NG6h-2 `tb_upsert_tenant` never returns NULL: it raises SQLSTATE
  `TB001` for a tombstone and `TB002` for a tenant with a non-terminal deletion (migration
  `3b8d6f2a4c17`), the DB-level twins of the `tombstone` and `deleting` reasons (`SqlTenants.deleted_error`
  maps them), and the worker drops a tombstoned installation's deliveries.
- **Grace window (decided, NG6h-1).** `tenant_deletions.not_before` (nullable, default NULL: no
  delay) is stored from the optional `tb_request_tenant_deletion` argument; the engine honours it.
- **Export before delete.** EB-15 lists "data export and deletion". A per-tenant data export is
  not designed here; the audit export (0032) and the read endpoints (0033) cover most of what a
  customer could ask for, and a dedicated export is a follow-up.
- **Backups.** Deleted rows persist in database backups until those expire; the backup
  retention is the deployment's (EB-6) and is documented in the Helm chart's notes (NG7). A
  restore also undoes a deletion that completed after the backup: the runbook replays those from the
  `tenant.deletion.completed` events (the SIEM export) before the worker and reconciler start.
- **Large tenants.** Batch size and a per-step time budget are settings (`DELETION_*`, NG6h-4a-3); a tenant with very
  many repositories makes the GitHub step long, which the resumable cursor handles.
- **The tombstone table (decided, user decisions B2 and B4, NG6h-3c-1).** `tenant_tombstones`
  (primary key `(host, installation_id)`, plus `tenant_id` and `deleted_at`) is the tombstone, apart from the
  deletion record, so the record can be purged (B4) without ending B2. It is host-scoped, outside
  RLS, ids only, with no foreign key. The app role has `SELECT` only and the owner role `SELECT,
  INSERT` (no `UPDATE` or `DELETE`); its only writer is `tb_finish_tenant_deletion`, which inserts
  the row (`ON CONFLICT DO NOTHING`) in the transaction that marks the deletion `done`. The readers
  (`tb_upsert_tenant` raising `TB001`, `SqlTenants`, `MemoryTenants`, `SqlTenantGate.standing` and
  through it the worker's drop) read the table; the in-flight rule (a `database`-step deletion whose
  tenant row is gone) stays on `tenant_deletions`. The migration (`4e9a1c7b5d23`) backfills the
  table from the existing `done` rows, idempotently, and is expand-only: the previous release still
  reads the `done` rows, which it leaves alone, so **the retention purge of the deletion records
  (NG6h-3c-2) must not run while a previous-release pod may exist**. A tombstone is one small row
  per deleted installation and is kept indefinitely: it is the guard against re-creation, not a
  record of the deletion, and holds no actor.
- **Deletion records' retention (decided, user decision B4, NG6h-3c-2).** `tenant_deletions` rows hold
  ids, counts and the requester's `sub` and `iss`. Retention is configurable: `DELETION_RETENTION_DAYS`
  defaults to **0, which keeps them indefinitely** (as before); a non-zero value must be at least
  `DELETION_RETENTION_MIN_DAYS` (default 90, as for `AUDIT_RETENTION_MIN_DAYS`) and negative values
  are rejected. The reconciler's singleton duty (`deletion-purge`, every `DELETION_PURGE_INTERVAL_SECONDS`)
  calls the definer `tb_purge_tenant_deletions(before, min_days, batch)` (migration `7b2d4f8a1c36`:
  `search_path` pinned with `pg_temp`, `EXECUTE` revoked from `PUBLIC` and granted to the app role,
  the validation of `tb_audit_purge`), in at most `DELETION_PURGE_MAX_BATCHES` batches of
  `DELETION_PURGE_BATCH_SIZE`, and stops starting batches after half the lease TTL.
  - **What is eligible.** `cancelled` records and `done` records older than the cut-off. A
    `done` record goes only when its `tenant_tombstones` row exists. A `cancelled` record needs no
    tombstone (a cancelled deletion left the tenant standing and a new request makes a new record);
    it holds the requester's identity, so it follows the same retention (storage limitation). Open
    and `failed` records are never purged: an operator may still resume or cancel a `failed` one, so
    it stays until it reaches a terminal state. A `done` record
    without a tombstone is skipped and counted (`unprotected`); the duty logs it at WARNING. The purge
    does not re-run the backfill and never deletes a `done` record whose tombstone is missing. The tombstones are
    never purged, so B2 holds at any retention.
  - **The composition hook.** `ReconcilerDeps.record_purger` (a `RecordPurger`, `deletion.ports`;
    `deletion.purge.SqlRecordPurger` is the implementation) is filled by NG6h-7's composition
    root; until then the duty is inert, like the deletion duty.
  - **The one-way door and the upgrade order.** The previous release (N-1) reads `done` records, and
    the repo's `schema_compat` integer only stops new code on an old schema, so nothing in the schema
    can say "no N-1 pods". The guard is the default: roll the release out fully, then set a
    retention. Once records are purged the downgrade of `4e9a1c7b5d23` refuses (its tombstones have
    no `done` record behind them), so a purge is a point of no return for rolling back past it. The
    downgrade of `7b2d4f8a1c36` itself only drops the function and the grant.
  - **`AUDIT_RETENTION_DAYS` and this retention are independent.** The `tenant.deletion.*` audit events
    (0032) carry the tenant id, host and installation id and stay until the audit purge removes
    them, whatever `DELETION_RETENTION_DAYS` is; with `AUDIT_RETENTION_DAYS=0` (never purge) they stay
    for ever, so the identifiers survive in the audit trail even after the record and (never) the
    tombstone. They hold no organisation login or tenant content (invariant 14), but `requested` names
    its actor. The record purge therefore shortens the table, not the audit
    trail; erasing the audit trail's identifiers is a decision for `AUDIT_RETENTION_DAYS`.
- **Identifiers that survive.** The tenant id, host and installation id remain in
  `tenant_deletions` and in the deletion's audit events until retention. They are treated as
  non-personal operational identifiers, not tenant content; this is a judgement call listed so
  the user can overrule it (the alternative is hashing them, which makes the record unusable
  for an operator).
- **Environment tracking (NG6h-6a, user decision B6).** The GitHub writer is the only code that creates
  an environment (`PUT /repos/{full}/environments/{name}`, which is a create-or-update and does not say
  which). With a tracker it first reads the environment (`GET`, 404 = absent) and, after the `PUT`, writes
  a `github_environments` row (migration `9a4d6c2e8b17`: `tenant_id`, `host`, `repository_id`,
  `environment_name`, `created_at`; primary key tenant, repository, name; tenant-scoped, no foreign key)
  only when the environment was absent. An environment found existing is never recorded, so it is never
  deleted; step 3 deletes exactly the tracked ones (`TrackedEnvironments.tracked`, then `forget` per
  deleted environment) and records the rest as `environment_kept`. Failure directions are all "kept": a
  crash or a failed write after the `PUT`, a release without the tracker (N-1), and an environment created
  before this migration are untracked. A row can also outlive its environment (someone deleted it and created their own of that name): NG6h-6
should delete a tracked environment only when GitHub's `created_at` for it is not later than the row's. The table is in the database step's `PLAN` (count `github_environments`), so a
  tenant's remaining rows go in step 5, and `tb_delete_tenant`'s emptiness backstop lists it (migration
  `9a4d6c2e8b17`, md5-guarded body replacement). The writer is wired in both composition roots, the
  api's `build_runtime` and the reconciler's `_core_deps`.

## Consequences

- EB-15 deletion exists as a tested, resumable operation rather than a runbook, with the same
  crash-safety model as credential operations.
- It is irreversible and reachable from the API; the preconditions (disabled first, typed
  confirmation, instance-scoped admin) and the audit trail are the safeguards.
- Operator-provisioned connection data is deliberately left to the operator, so deletion is
  complete on trustbridge's own state but not on the operator's secret store; the record says
  what remains.
- A new host-scoped table and a new column on `webhook_deliveries` are added outside RLS;
  deleting webhook bodies per tenant depends on the worker recording `tenant_id` at resolution.
- The audit trail keeps tenant references past deletion (user decision 3), which is the
  trade between auditability and erasure.
- Platform-side deletion depends on each adapter's delete semantics (0029), so the adapters'
  contract tests gain delete and revoke cases.

## Sources

- EB-3, EB-4, EB-6, EB-15 (the plan's "Enterprise baseline" rows).
- ADRs [0028](0028-trustbridge-architecture-and-instance-layout.md),
  [0029](0029-adapter-plugin-interface.md),
  [0031](0031-tenancy-and-row-level-security.md), [0032](0032-audit-log.md),
  [0033](0033-admin-api-v1-rbac-and-limits.md); NFR-0027.
- GitHub REST API, Actions secrets and variables, and deployment environments (deleting
  repository secrets, variables and environments with an installation token):
  https://docs.github.com/en/rest/actions/secrets,
  https://docs.github.com/en/rest/actions/variables,
  https://docs.github.com/en/rest/deployments/environments
- GDPR Art. 17: https://gdpr-info.eu/art-17-gdpr/
- Code read: `src/trustbridge/worker/models.py`, `worker/ports.py`, `adapters/models.py`.
