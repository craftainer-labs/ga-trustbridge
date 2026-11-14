# trustbridge/audit/

The append-only audit log ([ADR 0032](../../../docs/adrs/0032-audit-log.md), Accepted
2026-11-04 with #18 and #21; the remaining flows arrive with NG6e and NG6h (the read routes and the export arrived with NG6d), per the
deferred table below): the storage layer (NG6c), event value validation, redaction and the
emitters at the call sites that exist today (NG6f). Out of scope
here: the mutating endpoints' events (NG6e)
and the deletion's request path (the admin routes, NG6h-7b). SIEM push is a follow-up.

- `model.py`: `AuditEvent` (what to write; validates every value at construction), `AuditRecord`
  (what is read; `to_json` and `to_ndjson_line` are the export shape), `AuditQuery` (filters and
  the `seq` cursor), `AuditPage`.
- `actions.py`: `Action`, the closed set of actions, and `SystemActor`, the fixed system names.
- `redact.py`: `project` (allow-list per shape), `scrub_text`, `truncate`, and the shared policy
  helpers `printable` and `has_secret_word` (`SECRET_WORDS`), also used by `admin/views.py`. The shapes
  added for the admin mutations are `tenant_admin` (the admin-disable columns), `delivery` (retry state,
  never headers or the body) and `connection` (`{"configured": true}` only; no route writes connection
  data, so it has no emitter). `host_registration` (`kind`, `app_id`, `app_slug`, `webhook_secret_ref`),
  for the registration events. `failure` (`reason`, one `FailureReason` code; `failure_after`)
  is the `after` of a failed registration or rotation: a fixed code, never a message.
- `denial.py`: `Denial` and `DenialSink`, a dependency-free leaf. `trustbridge.rbac` imports them
  from here (re-exporting them); `audit` never imports `rbac` (an import-linter contract), so the
  worker, which imports `audit`, does not load the FastAPI app.
- `emit.py`: `AuditWriter`, `with_trace`, `current_trace_id` and the sinks `TenantSyncAudit` and
  `AuditDenials`.
- `credentials.py`: `credential_events` and `CredentialAuditObserver` (imported by name, not
  re-exported, because it depends on the adapter core's types).
- `store.py`: the functions below.

## Emitting an event

1. **Build an `AuditEvent`.** Every value is validated when it is built and a bad one raises
   `ValueError`, so an event that cannot be validated is never written (fail closed):
   - `actor_type` and `outcome` must be the enum members; `action` must be an `Action` member
     (its string value is accepted and stored as the member); a `system` actor's `actor_sub` must
     be a `SystemActor` name and has no `actor_iss`; a `user` actor needs `actor_sub` and
     `actor_iss` (the token's, never email, login or display name).
   - Text columns are printable strings within `TEXT_LIMITS` (`actor_sub` 255, `actor_iss` 512,
     `resource_type` 64, `resource_id` 255, `request_id` 128, `trace_id` 128,
     `source_ip` 64); `source_ip` is an IP address without a scope id; `tenant_id` and `id` are
     UUIDs; `schema_version` is an integer from 1 to `SCHEMA_VERSION`.
   - `on_behalf_of` is `None` or exactly `{"github_sender_id": <non-negative int, not bool>,
     "delivery_id": "<printable string, at most MAX_DELIVERY_ID = 100>"}` (user decision).
   - `before` and `after` are `None` or a JSON object that `jsonb` accepts (no NaN, no NUL),
     at most `MAX_PAYLOAD_BYTES` serialized.
2. **Build `before` and `after` with `project(shape, source)`**, never by dumping a row. The
   allow-list per shape lives in `redact.ALLOW_LISTS` (`tenant`, `credential`, `oidc_trust`,
   `authz_denial`); a field not on it is absent, an unknown shape raises, values are scalars and
   strings are cut at 256 characters with a marker. Free text (an error message) goes through
   `scrub_text` first. Add a field to the list only after deciding it can never hold a secret.
3. **Write it, in the right transaction.**
   - Joins a change: `await append(session, with_trace(event))` in the change's own session,
     opened inside `tenant_context(tenant_id)` (the insert policy admits the context tenant or
     null). The audit row then commits with the change or not at all. No `RETURNING`.
   - Stands alone (a denial, a sync, a sweep): `await AuditWriter(factory).record(event)`, which
     opens its own short transaction inside the event's tenant context.
   - `with_trace` fills `trace_id` from the current OpenTelemetry span; `AuditWriter` calls it.

A tenant event (`tenant_id` set) must be written inside that tenant's context. An event with a
null `tenant_id` (`authz.denied`, instance-level events) is written with no context.

## Emitters on main today

| Action | Where | Actor | Transaction |
| --- | --- | --- | --- |
| `tenant.sync` | `github.handlers.InstallationHandlers` (`created`, `deleted`, `suspend`, `unsuspend`) through `TenantSyncAudit`, wired in `worker.runtime.build_core` | `github-webhook`, `on_behalf_of` = `{"github_sender_id": <numeric id>, "delivery_id": "<id>"}` (JSON object; null, not an empty object, when the payload has no numeric sender) | Own, after the tenant change and the repository changes. A failure fails the handler, the worker retries, and the sync repeats (a retry of the same delivery is deduplicated by the idempotency key; a change is never left without one). `after` = `{"state"}`; `before` is not recorded (the sync overwrites the state from GitHub's current view). |
| `authz.denied` | `rbac.deps.require_role` through `RbacRuntime.denials` (`AuditDenials`), for a role that is missing and for a tenant that is unknown, malformed or not granted (answered 404) | the denied user (`sub`, `iss`), `source_ip` from the ASGI client, `request_id` from a plain `X-Request-ID` | Own, null `tenant_id`. Instance route: `resource_type=role`, `resource_id=<required role>`. Tenant route: `resource_type=tenant`, `resource_id=<requested tenant id>` (none if the path value is not a canonical UUID). If the sink fails the request fails. Unauthenticated rejections are never rows. |
| `credential.rotate.begin`, `credential.rotate.finish`, `credential.issue`, `credential.revoke`, `credential.operation.abandoned`, `oidc_trust.create`, `oidc_trust.remove` | `adapters.sql_store.SqlCredentialStore.save` through its `observer` (`credentials.CredentialAuditObserver`) | `worker` or `reconciler` (chosen at construction) | The save's own, so the event commits with the credential state. See `credentials.py` for the transition table. |
| `credential.rotate` (admin call, success) | `credentials.CredentialAuditObserver.saved`, for the call that finishes a rotation (`join_call_event` / `JoinedCallEvent`); otherwise `admin.credentials` after the core returns | the caller | Appended in the finishing save's own transaction, so a finished rotation never commits without it (invariant 9, NG6i); if the append fails the save rolls back and the call fails. |
| `credential.orphan.delete` | `adapters.core.CredentialCore.sweep_orphans` through `CoreDeps.orphans` | as above | Own; one event per platform credential deleted. If the write fails the sweep still finishes, logs each unaudited orphan at error level, then raises. `resource_id` is the platform name. |
| `audit.purge` | written by `tb_audit_purge` itself (actor `reconciler`) | | The purge's. |
| `tenant.deletion.step`, `tenant.deletion.skipped`, `tenant.deletion.completed`, `tenant.deletion.failed` | `trustbridge.deletion` (`deletion/audit.py`), through `SqlDeletionStore` | `reconciler`; `resource_type=tenant_deletion`, `resource_id` the deletion id | The transition's own: each definer transition (`tb_advance`, `tb_finish`, `tb_fail`) and its events are in ONE session transaction, opened inside `tenant_context(tenant_id)` (the `completed` event inserts after the tenant row is gone). `before` and `after` use the `deletion` shape (ids, state and step names, a category, a count, a fixed code) plus `counts` through `deletion_counts`; never row content or the organisation login. |

`CredentialCore` is not instantiated by any process yet (NG5/NG6), so the credential events are
wired by whoever builds it: `SqlCredentialStore(session_factory, CredentialAuditObserver(writer,
actor))` and `CoreDeps(..., orphans=observer)`. `trustbridge.main` wires `RbacRuntime(config, access, ThrottledDenials(AuditDenials(AuditWriter(
session_factory)), counter, ...))` when it builds the admin runtime.

### Bounding `authz.denied` (`throttle.py`)

A principal with a valid token can ask for random tenant ids and be denied each time, so the sink
is wrapped in `ThrottledDenials` (NG6d decision A6). Per (subject, reason) and fixed window
(`AUDIT_DENIED_WINDOW_SECONDS`, default 60) the first `AUDIT_DENIED_BURST` (default 5) denials are
recorded in full: the 5th is a row, the 6th is only counted (in the counter store and the metric
`trustbridge.audit.authz_denied.suppressed`). After the window has closed, a summary
`authz.denied` row is written: `after` carries `suppressed` (how many were not recorded),
`window_seconds`, `window` (the window number, `time // window_seconds`) and `"sample": "last"`;
`resource_id` is null, and the denied principal, `reason`, `required_role`, `scope`, `tenant_id`,
`source_ip` and `request_id` are those of the LAST suppressed denial of the window.
`"sample": "last"` says exactly that: those fields describe one request, not the whole burst.
The window count lives in Valkey (shared by the replicas, key
`tb:audit:denied:<hash of sub and reason>:<window>`).

**Delivered at least once, never lost.** Every suppressed denial notes its window in a Valkey
index (one call): the set `tb:audit:denied:pending` of subject/reason hashes and, per hash, the set
`tb:audit:denied:pending:<hash>` of window numbers, and stores itself as the window's sample
(`...:<window>:sample`, so the sample is the last suppressed denial). All of it is kept
`max(2 * window, 1 day)` and nothing is kept in the process. A summary is written by either path,
both reading that index:

- the first denial of the same (subject, reason) in a later window summarises EVERY closed pending
  window of the pair, however many windows ago. The current denial is recorded first; each old
  summary is then tried on its own, and a failure is logged, never raised, so an old summary cannot
  fail a request;
- `ThrottledDenials.flush()` summarises every closed pending window of every subject. The api calls
  it at startup, every window, and at graceful shutdown (bounded to a few seconds), so a restarted
  or second replica finishes the other's work, and a flush the shutdown cut short is continued by
  the next start. The still-open window is never summarised early. Hash entries are not removed
  eagerly; they expire, and what a flush over the pending-base set costs is measured in NG6g.

There is no claim before the write. A replica reads the window, writes the row, and only then marks
the window done (it stores the count it summarised) and removes it from the index. So a crash, a
timeout, a cancel or a failing sink at any point leaves the window pending and the next pass writes
it. The price is that **a summary can be written twice** when two replicas handle the same window
at the same moment: the duplicates are recognisable, because `after` has the same `window` (and the
row the same actor and `reason`) and the same `suppressed`. A denial counted after a summary was
written (a request that straddled the window end) puts the window back in the index and gets its own
smaller summary, with the same `window`, `suppressed` being only what was new. A window that closes
with nothing suppressed writes no summary.

The only loss: the whole counter store (Valkey) down. Then no count exists, so the throttle fails
open and EVERY denial is recorded as a row (the trail is complete, only larger). A timeout or error
on a single call also records that denial itself (`AUDIT_DENIED_TIMEOUT_MS`, default 50), at the
price that its summary may count it once more.

## Read and export routes (NG6d)

`trustbridge.admin` serves `GET /admin/v1/audit-events`, `/audit-events/export` and the same two
under `/tenants/{tenant_id}` to the `auditor` role (see `admin/README.md`). The export event:
`record_export` (`export_event.py`) writes one `audit.export` event per export request, after the
stream ended or was cut off, in its own transaction (in the tenant's context for a tenant export):
the reader's `sub` and `iss`, `resource_type=audit_events`, `resource_id` the tenant id or
`instance`, and in `after` the scope, the filters of the request (`after_seq`, `since`, `until`,
`actor_sub`, `action`, `resource_type`, `resource_id`, `outcome`, the instance route's `tenant_id`),
`row_count`, `last_seq` and `complete`. An export that failed or whose client went away is outcome
`failure` with `complete: false` and `last_seq` to resume from.

## Deferred events

ADR 0032 lists events whose flows do not exist on main yet. Their `Action` members exist where the
ADR names them, with no emitter:

| Event | Owner |
| --- | --- |
| (none left for the admin API.) The router walk `admin.guards.mutations_without_emitter` fails any mutating route without an `emits` marker. Emitted since NG6e-2 to NG6e-5: `credential.rotate`, `tenant.disable`, `tenant.enable`, `tenant.reconcile`, `delivery.requeue` and `host.registration.start`, `.manual`, `.complete` (the `host_registration` shape); the credential events of a rotation made through the API have the system actor `api` | done |
| `tenant.deletion.requested` and `.cancelled` (0034) | NG6h-7b (the admin routes) |
| The reconciler duty calling `purge_expired` (ADR 0032 "Retention") | done (NG6h-3a): the `audit-purge` job |
| A `transaction_timeout` (or statement and idle-in-transaction timeouts) on the api, worker and deletion connections, and the startup check of `AUDIT_EXPORT_LAG_SECONDS` against it (ADR 0032 "Export") | done (NG6h-3b): the `after_begin` hook and `tenancy.timeout_check`, see `tenancy/README.md` "Transaction time bound" |

An operation ending `failed` (rolled back, previous state restored) has no event of its own
because ADR 0032 does not list it; the inverse `oidc_trust` event shows a rolled-back kind change.

## API

All take an `AsyncSession` (or factory) and leave the transaction to the caller, except
`purge_expired`.

| Function | Use |
| --- | --- |
| `append(session, event) -> UUID \| None` | Insert in the caller's transaction, so the row commits with the change. No `RETURNING` (a null-tenant row is not selectable by the app role); the id is generated in code, and the insert count says whether the row was written. `None` means a redelivered webhook's event already exists (see "Idempotency key"); no caller uses the id. `tenant_id` must be null or the session's tenant context. |
| `list_events(session, query, *, tenant_id=None, instance_scope=False) -> AuditPage` | One page. `tenant_id` set: that tenant (open the session in its `tenant_session`; the tenant is also filtered explicitly). `instance_scope=True` with `tenant_id=None`: instance scope through `tb_audit_list()`, all tenants and null-tenant events; the caller must have checked the instance-scoped `auditor` role, this layer does not. `tenant_id=None` without the flag, or both together, raises `ValueError` before any query, so a failed tenant lookup never widens a read. NG6d passes the flag only after the role check. |
| `get_event(session, event_id) -> AuditRecord \| None` | One event by id, any tenant (null-tenant included), through `tb_audit_get()`. Instance scope like `list_events(instance_scope=True)`: the caller must have checked the instance-scoped `auditor` role. Imported from `trustbridge.audit.store`. |
| `export_ndjson(session, query, *, tenant_id=None, instance_scope=False, lag_seconds, page_size) -> AsyncIterator[bytes]` | Ascending `seq`, one line per event, resumable with `AuditQuery.after_seq`. Same scope rules as `list_events`. |
| `purge_batch(session, *, before, min_days, batch_size)` | One `tb_audit_purge` call; returns rows deleted. |
| `purge_expired(factory, *, retention_days, min_days, batch_size, max_batches, on_batch, budget_seconds, monotonic, now)` | Loops `purge_batch`, one short transaction per batch, at most `max_batches` per call; `retention_days=0` never purges. Raises `ValueError` before any transaction for a negative retention, a non-zero retention below `min_days`, a `batch_size` outside 1..10000 or `max_batches < 1`. `on_batch(deleted)` runs after each committed batch with its count; `budget_seconds` stops new batches once that much time has elapsed. For the reconciler's singleton duty. |

**Cursor.** `AuditPage.next_seq` is the last item's `seq` when more follow, else `None`. Continue
an ascending read with `AuditQuery(after_seq=next_seq)`, a descending one
(`descending=True`) with `before_seq=next_seq`. The admin API wraps `seq` and the event id in an opaque cursor.
`limit` is 1 to 1000.

**Export lag.** `lag_seconds` makes a read stop before the first event whose `recorded_at` is
newer than the lag (not merely skip such events), so a consumer resuming from the last `seq`
never skips a late commit. It is a consistency model, not real time; writers must run with a
transaction timeout below the lag. Nothing on main sets such a timeout yet, so the startup check
of the lag against it is deferred (see "Deferred events").

**NDJSON shape.** One compact JSON object per line, `\n`-terminated, keys in column order:
`id, seq, occurred_at, recorded_at, tenant_id, actor_type, actor_sub, actor_iss, on_behalf_of,
source_ip, action, resource_type, resource_id, outcome, before, after, request_id, trace_id,
schema_version`. Timestamps are UTC with a `Z`; ids and `source_ip` are strings; `on_behalf_of`,
`before` and `after` are nested JSON objects (or null). The route sets
`application/x-ndjson` and writes the export's own audit event.

## Database side (migrations `e5c9a37d2b18`, `f6d2b8c14a39`)

`f6d2b8c14a39` changes `on_behalf_of` from `text` to `jsonb` (user decision: structured, not a
`key=value` string). It is N-1 compatible because the previous release never touches
`audit_events` and nothing wrote the column before this release (no backfill needed; a stray
`sender=..;delivery=..` string is converted to the object, any other stray value to null). It
also adds `ck_audit_events_on_behalf_of`: the database accepts only null or an object with a
numeric `github_sender_id` and a string `delivery_id`. No roles, no grants.

- `audit_events` with indexes `(tenant_id, seq)`, `(occurred_at)`, `(actor_sub, seq)` and, added
  here, `(recorded_at)` (the lag bound looks up the first recent event).
- **Grants:** `trustbridge_app` has `SELECT` and `INSERT` only.
- **Triggers:** `BEFORE UPDATE OR DELETE` (row) and `BEFORE TRUNCATE` raise unless
  `current_user = 'trustbridge_owner'`; `BEFORE INSERT` stamps `occurred_at` and `recorded_at`.
- **Purge:** `tb_audit_purge(before, min_days, batch)` (`SECURITY DEFINER`, fixed `search_path`)
  deletes up to `batch` oldest rows past the cut-off, refuses a cut-off newer than
  `now() - min_days`, and writes an `audit.purge` summary event when it deleted anything.
  `min_days` is passed by the caller (from `AUDIT_RETENTION_MIN_DAYS`), so it guards typos; it
  is not a defence against a compromised app role.
- **Read:** `tb_audit_list(...)` (`SECURITY DEFINER`) for instance scope.
- `EXECUTE` on both is revoked from `PUBLIC` and granted to the two roles.
- **RLS** is enabled and forced from creation (ADR 0031 "seventh RLS table"), but only if both
  roles exist, as for every grant and policy. App role: `INSERT` with a null or the context
  tenant, `SELECT` of the context tenant's rows. Owner: `owner_all`.
- Definer functions are owned by whoever runs the migration; setting the owner explicitly is
  deferred to release N+1 for all definer functions. For the purge to pass the trigger, that
  owner must be `trustbridge_owner` (the migrate Job's role, ADR 0028); the integration test
  re-owns them to emulate it.

## Idempotency key (migration `8c4e1a7d3b52`, NG6h-2b)

A webhook-driven event is the one with `on_behalf_of` set. `(tenant_id, action,
on_behalf_of->>'delivery_id')` is unique where `on_behalf_of IS NOT NULL` (the partial index
`ux_audit_events_webhook_delivery`), and `append` inserts `ON CONFLICT (...) WHERE on_behalf_of IS
NOT NULL DO NOTHING`, matching the index definition exactly so PostgreSQL infers it. A redelivery,
or a worker retry after the audit write committed, therefore adds no second row; `append` and
`AuditWriter.record` return `None` for it. Any other unique violation (the primary key) still
raises. Events with a null `on_behalf_of` (admin, worker, denials, purge, and a webhook event with
no numeric sender) are never deduplicated, and neither is a webhook event with a null `tenant_id`
(NULLs are distinct; no emitter writes one). `append` sends `ON CONFLICT` only for a keyed event
(`on_behalf_of` and `tenant_id` set): a conflict target needs `SELECT` on its columns, so the
SELECT policy would also be applied to the new row, which a null-tenant row (a denial) fails.

The migration is an index only, built `CONCURRENTLY` (no table rewrite, writes continue), so it is
N-1 compatible: the previous release's inserts keep working. It refuses to run, changing nothing, if
the table already holds duplicates for the key (the log is append-only and migrations never edit
history); the error names the count and the query. Reconcile them as the table owner, then upgrade
again. A failed build leaves an INVALID index, which the next upgrade drops and rebuilds.

### Reconciling existing duplicates (operator procedure)

Needed only when the upgrade stops with `audit_events holds N duplicate webhook delivery
group(s)`. A duplicate is a redelivered webhook's repeated event (same tenant, action and
delivery id). Nothing has changed at that point: no index, no rows.

1. List the groups (the query the error prints; null-tenant rows are not keyed, so they are not
   listed):

   ```sql
   SELECT tenant_id, action, (on_behalf_of->>'delivery_id') AS delivery_id, count(*) AS n
   FROM audit_events WHERE on_behalf_of IS NOT NULL AND tenant_id IS NOT NULL
     AND (on_behalf_of->>'delivery_id') IS NOT NULL
   GROUP BY tenant_id, action, (on_behalf_of->>'delivery_id') HAVING count(*) > 1;
   ```

2. Review them. For the same delivery the rows record the same fact twice.
3. Connect as `trustbridge_owner` (the migrate Job's role): the append-only guard and the RLS
   `owner_all` policy let only that role delete. Keep the earliest row of each group and archive the
   rest in one transaction, so nothing is lost:

   ```sql
   BEGIN;
   CREATE TABLE audit_events_duplicates_archive AS
   SELECT * FROM (
     SELECT e.*, row_number() OVER (
       PARTITION BY tenant_id, action, (on_behalf_of->>'delivery_id') ORDER BY seq) AS rn
     FROM audit_events e WHERE on_behalf_of IS NOT NULL AND tenant_id IS NOT NULL
     AND (on_behalf_of->>'delivery_id') IS NOT NULL) d
   WHERE rn > 1;
   DELETE FROM audit_events WHERE seq IN (SELECT seq FROM audit_events_duplicates_archive);
   COMMIT;
   ```

   The `seq` gaps are harmless: exports resume with `after_seq`. Keep or drop the archive table
   under your retention policy; it is outside the app's grants. If the transaction aborted, nothing
   was created or deleted, so rerun it; if you rerun after a committed attempt, pick another archive
   table name.
4. Run the upgrade again (`alembic upgrade head`, or re-run the migrate Job). If an earlier
   attempt left an INVALID `ux_audit_events_webhook_delivery`, the upgrade drops it first. Check
   with `SELECT indisvalid FROM pg_index WHERE indexrelid = 'ux_audit_events_webhook_delivery'::regclass;`.

The duplicate check scans the table under a 120 s statement timeout; on a very large table that
timeout fails the upgrade before anything changes, and the check can be run by hand (step 1) first.

## Settings

`AUDIT_RETENTION_DAYS` (365; `0` never purges; otherwise at least the floor),
`AUDIT_RETENTION_MIN_DAYS` (90), `AUDIT_EXPORT_LAG_SECONDS` (30, which `TRANSACTION_TIMEOUT_SECONDS`, 15, must stay below), and for the purge duty
`AUDIT_PURGE_INTERVAL_SECONDS` (3600), `AUDIT_PURGE_BATCH_SIZE` (1000, at most 10000) and
`AUDIT_PURGE_MAX_BATCHES` (100 batches per run, so a run holds its lease for a bounded time; the
next run continues).

The reconciler's `audit-purge` job (`Reconciler.audit_purge`) holds the `audit-purge` lease (one
replica at a time), beats its heartbeat and counts rows in the `trustbridge.audit.purged` metric per batch (a
mid-run failure loses none), logs a total at the end, stops starting batches after half the
lease TTL, is a no-op when retention is 0, and logs once at startup when there is no audit store. It writes no audit row itself:
`tb_audit_purge` writes the `audit.purge` summary event per batch.

## Rules

- `AUDIT_RETENTION_DAYS` equal to `AUDIT_RETENTION_MIN_DAYS` passes validation but leaves no
  clock-skew margin: an app clock ahead of the database's makes every purge raise 22023. Keep
  retention above the floor by a margin. `AUDIT_EXPORT_LAG_SECONDS` has no upper bound.
- Redaction is the emitter's job (`redact.project`); `append` stores `before` and `after` as
  given, after the construction-time shape and size checks.
- Never `RETURNING` on an insert; never write `occurred_at`; never delete except via the purge.
