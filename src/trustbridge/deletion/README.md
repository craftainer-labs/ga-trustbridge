# trustbridge/deletion/

The tenant deletion engine ([ADR 0034](../../../docs/adrs/0034-tenant-data-deletion.md), NG6h-4a-2):
the state machine, the lease and fence loop, the failure policy, the drain step and the audit
events. The database half is migration `5d7a2c8e1b94` (`tenancy/README.md`). The reconciler duty
(`worker/reconciler.py`, `tenant_deletion`), the `DELETION_*` settings, the B10 gate
(`tenancy/gate.py`) and the [runbook](../../../docs/runbooks/tenant-deletion.md) are NG6h-4a-3; the
three steps are filled by NG6h-4b (database), -5 (platform, done) and -6 (GitHub, done). Until then the steps are
Protocols and the tests use doubles, and nothing composes a `DeletionEngine` in a process: the
reconciler runs the duty only when it is given a `DeletionDriver` (`ReconcilerDeps.deletion`,
`ProcessOverrides.deletion`).

The duty is inert in production until the steps exist: no deletion can run with a step missing. The composition
root that builds the `DeletionEngine` with the real steps and hands it to the reconciler lives outside the worker
package and comes in NG6h-7, once 4b, 5 and 6 have landed.

- `ports.py`: `DeletionDriver` (`run_once() -> bool`), `RecordPurger` and `PurgeResult` (NG6h-3c-2). The only module the worker and reconciler
  may import (import-linter contract 3, which fails if that import ever disappears); it imports
  nothing from the package, and `__init__` re-exports nothing, so depending on it loads no engine
  code.
- `engine.py`: `DeletionEngine(store, EngineSteps, EngineConfig)`, the `DeletionDriver`.
- `model.py`: `DeletionRecord`, `Lease`, `StepContext`, `StepOutcome`, and the control-flow
  exceptions `LeaseLost`, `RunBudgetExhausted` and `NonRetryableError`.
- `steps.py`: the `Step` Protocol and the slots `PlatformStep`, `GithubStep`, `DatabaseStep`, plus
  `WritersPort` for the drain.
- `drain.py`: `DrainStep`.
- `platform.py`: `AdapterPlatformStep`, the `PlatformStep` (NG6h-5), over `PlatformPort` and `ConnectionNamesPort`. See "The platform step" below.
- `github.py`: `GithubDeletionStep`, the `GithubStep` (NG6h-6), over `GithubPort`. See "The GitHub step" below.
- `database.py`: `SqlDatabaseStep`, the `DatabaseStep` (NG6h-4b). See "The database step" below.
- `store.py` / `sql.py`: `DeletionStore` and `SqlDeletionStore` over the definer functions.
- `purge.py`: `SqlRecordPurger`, the `RecordPurger` over `tb_purge_tenant_deletions` (NG6h-3c-2). See "Record retention" below.
- `audit.py`: the event builders. `metrics.py`: `trustbridge.deletion.attempts`, `.failed` and `.records_purged`.

## One run

`run_once()` claims one runnable deletion (`tb_claim_tenant_deletion`: lease free or expired,
`not_before` and `retry_at` passed, fence bumped) and walks it `requested > draining > platform >
github > database > done`. Before every pass it renews the lease. A step returns a `StepOutcome`:
`counts` are added to the record's, `skipped` entries (`category`, `reason`, `count`) are appended,
and `done=False` with a `cursor` saves progress in the same state and runs the step again. A save whose cursor
changed also clears `attempts`, `retry_at` and `last_error` in the database (migration `6f3b9d2e4a85`); an unchanged
cursor leaves them alone.

- **Lease and fence.** Every write goes through a definer function carrying `(id, holder, fence)`.
  `TB003` (lease lost) and `TB004` (terminal or invalid) both raise `LeaseLost`: the engine stops
  this deletion at once and writes nothing more (the new holder owns it).
- **Budget.** The run ends, releasing the lease, once `budget_fraction` (default 0.5) of the lease
  TTL has passed since the claim, checked before each pass and in `StepContext.renew`, which
  raises `RunBudgetExhausted` for a long step. Renewals are unbounded by design, so this is what
  keeps a run from outliving its lease. The state and cursor are kept; the next claim resumes.
- **Failure policy** (NG6h-4a ruling 5). Any exception from a step or from the store is
  transient: `tb_retry_tenant_deletion` records the scrubbed `last_error`, counts the attempt,
  sets `retry_at = now + min(cap, base * 2**attempts)` and frees the lease; the engine increments
  `trustbridge.deletion.attempts` (tag `step`), and `tenant_deletions.attempts` is the alertable
  value. `failed` is written only for a `NonRetryableError(code, message)` (`tb_fail`, with the
  lease) or by an operator (`tb_operator_fail`, NG6h-7). An invalid step output (a `ValueError`
  while the audit events are built, including a bad `counts` value, checked on every pass so it is
  never saved) is a step bug and becomes `NonRetryableError("invalid_step_output")`. A blocked step holds the deletion; it
  never advances on its own. An error recording the retry or the failure itself (the database
  down) propagates from `run_once`; the reconciler's duty logs it and ends that poll, the lease
  then lapses and the deletion is claimed again.
- **Draining.** `DrainStep` calls `WritersPort.quiesce(tenant_id)` (re-scan `webhook_deliveries`
  recorded for the tenant and its `credential_operations`, abandoning in-flight ones) until two
  consecutive passes find nothing, every `poll_seconds`, at most `max_passes` passes
  (`DrainNotQuiescent`, transient, otherwise). There is no writer-barrier column: the `deleting`
  refusals stop new writes and `tb_delete_tenant` holds the installation's advisory lock.
- **Finish.** After the `database` step the engine calls `DeletionStore.finish`: in one
  transaction `tb_delete_tenant(tenant, holder, fence)` (the `tenants` row, last), then
  `tb_finish_tenant_deletion` (`done`, and the `tenant_tombstones` row, in that one function) and the audit events. The `DatabaseStep`
  therefore clears everything except the `tenants` row.

## The database step

`SqlDatabaseStep(factory, batch_size=500)` deletes the tenant's rows in `PLAN` order (`credential_operations`,
`adapter_credentials`, `adapter_resources`, `scope_leases`, `github_environments`, `tenant_repositories`, then clears
`webhook_deliveries` bodies), never the `tenants` row. One pass is one batch of one table in one transaction inside
`tenant_context(tenant_id)`; a short batch with nothing of the tenant left in the table moves the cursor (`{"table", "removed"}`) to the next table, a full one asks
for another pass on the same table. A cursor it cannot read starts over, which is safe because each pass deletes whatever
is left.

- **Primary-key batches (NG6h-4c).** A batch is `DELETE FROM t WHERE (pk) IN (SELECT pk FROM t WHERE tenant_id = :tenant
  ORDER BY pk LIMIT :limit FOR UPDATE SKIP LOCKED)` (the `webhook_deliveries` UPDATE adds `status <> 'deleted'`; the keys are picked in a `MATERIALIZED` CTE so the limit holds), with the
  key from `database.PRIMARY_KEYS` (composite for most tables; an integration test compares it with `pg_index`). It is not
  keyed on `ctid`, which a concurrent update changes, so an updated row could be skipped. `SKIP LOCKED` passes over rows
  another transaction holds instead of waiting, so a pass can remove fewer rows than the batch while rows are left; a short
  pass therefore checks, in the same transaction, whether the tenant still has a row in the table (for deliveries, one not
  yet `deleted`) and, if so, keeps the cursor on the table: the next pass takes the rows once their holder commits. The
  step moves to the next table only when the table holds nothing of the tenant's. A holder that never lets go keeps the
  step on that table until the run budget ends the run (each pass is cheap); `tb_delete_tenant`'s emptiness check stays the
  backstop for a row written after the step passed the table.
- **No definer functions.** The app role already holds `SELECT, INSERT, UPDATE, DELETE` on the five tenant tables
  (`tenant_repositories`, `scope_leases`, `adapter_resources`, `adapter_credentials`, `credential_operations`:
  `KEYED_TABLES` in `d4b8f1e60a27`, granted in `5d1f3a7c9e24` and `c3a5e7d91f42`) and on `webhook_deliveries`
  (`c3a5e7d91f42`), and each keyed table's `tenant_isolation` policy is `FOR ALL` with `USING (tenant_id = (SELECT
  tb_current_tenant()))` and the same `WITH CHECK` (`d4b8f1e60a27`), so the step adds no privilege. The `UPDATE` grant
  and a policy that covers `UPDATE` are load-bearing, not only `DELETE`: `SELECT ... FOR UPDATE` requires `UPDATE`
  privilege (PostgreSQL `SELECT`, "The Locking Clause"), and under RLS the `UPDATE` policies apply to the rows it
  locks (`ddl-rowsecurity`), so without them the `picked` CTE would return no rows and the step would stall.
  Preconditions: the migrations grant and create the policies only if the `trustbridge_app` role exists when they run
  (roles are created outside migrations), and RLS is created inert (no `ENABLE`/`FORCE ROW LEVEL SECURITY` until
  release N+1), so the integration test, which enables and forces it itself, is what exercises this path today.
  `webhook_deliveries` is outside RLS and has no policy. Every pass starts with a lease guard in the same transaction (a `SELECT` on `tenant_deletions` for this
  deletion's holder, fence and an unexpired lease, state `database`; the app role cannot lock that table, so it is a
  check, not a lock): a stale holder gets `LeaseLost` and touches nothing. A holder that loses the lease just after the
  check deletes at most one more batch of the same rows the new holder would delete, and `tb_delete_tenant` re-checks
  that nothing is left (including any `webhook_deliveries` row of the tenant not yet `deleted`).
- **A late delivery** (a webhook accepted for the tenant after the step cleared its rows) makes
  `tb_delete_tenant` refuse with `55000`; the engine retries the database step, which marks it `deleted`, so the deletion
  converges. NG6h-7's end-to-end test must cover a delivery that arrives during a deletion.
- **Webhook deliveries** recorded for the tenant keep their row with status `deleted` and the dedupe key (`host`, `delivery_id`, `body_sha256`) and the
  non-personal `event`; `body`, `headers` (allow-listed request headers; NOT NULL, so `{}`) and `last_error` (handler
  error text, which may name tenant data) are cleared. Nothing reads the headers of a `deleted` row.
  Rows with no `tenant_id` are left to retention; the last pass counts those still holding a body on the deletion's host
  and reports them once as `skipped` `webhook_bodies_unresolved` / `retention_only` with a `count`. That count is
  host-wide: an unresolved delivery cannot be attributed to the tenant, so it is an upper bound.
- **Retained:** `audit_events` and `tenant_deletions` (ADR 0034).
- **Category names** in `counts` are `adapter_operations`, `access_grants`, `adapter_resources`, `scope_leases`,
  `github_environments`, `tenant_repositories` and `webhook_bodies`: the tables `credential_operations` and `adapter_credentials` contain a
  secret word, so their counts take other names. A new tenant-keyed table is added to `database.PLAN`; an integration
  test fails when a table with a `tenant_id` column is in neither `PLAN` nor the retained set.
- The batch size is a constructor argument; the composition root (NG6h-7) chooses it.

## The platform step

`AdapterPlatformStep(port, connections)` (NG6h-5). `PlatformItem` and `ItemKind` live in `ports.py` so
the worker can build the port without importing the engine. The worker's `SqlPlatformPort`
(`worker/platform_deletion.py`, composed by `build_platform_port` in `worker/tenant_reconcile.py`
from the same collaborators as the reconcile body, plus a `ResourceCore`) is the real port.

- **A pass tries every recorded item once**, in this order: credentials, webhooks, repositories,
  projects (NG6h-5b). An item that fails is skipped for the rest of the pass and the pass moves on,
  so one stuck item never holds up the ones behind it. A project is never tried while one of its
  repositories (`<project>/<repo>`, same adapter) remains. A pass stops after `MAX_TRIES_PER_PASS`
  (50) tries, or when `ctx.renew` raises `RunBudgetExhausted` (what was removed is still reported;
  with failures and nothing removed the step holds, so the attempt is counted; with neither the
  exception goes to the engine). A pass starts at item `attempts % len(items)` and wraps, so items
  that keep failing at the head cannot starve the ones behind them across retries. The cursor is `{"removed": n}` and
  changes only when something was removed, so the engine resets the attempts exactly on progress.
  The list comes from the recorded rows only (`adapter_credentials` rows that still hold a
  credential, `adapter_resources` rows) and is re-read on every pass: a restart converges, nothing
  unrecorded is ever removed, and a credential row left with no credential is not listed again.
- **Credentials** go through `CredentialCore.delete_scope(tenant, adapter, scope, deletion_id=<the
  deletion's id>, remove_outputs=False)`: write-ahead, under the adapter lease, admitted by the B10
  gate for this deletion only. `remove_outputs=False` leaves the GitHub outputs recorded for the
  GitHub step and makes no GitHub call. **Resources** go through `ResourceCore.delete` (no gate,
  not write-ahead: the adapter's `delete` is idempotent).
- **`delete_platform_resources`** (user decision 1): repositories and projects are removed only
  when the record's flag is set; otherwise they are listed in the last pass as
  `skipped` `platform_repositories` / `platform_projects` with reason `kept_by_request`.
- **Operator-owned connection data** (user decision 4; the Argo CD token belongs here): the last
  pass records one `skipped` entry per adapter name found under `store:tenants/<uuid>/`
  (`operator_owned_connections`, reason `operator_owned`, `adapter`, `count` 1). Only names are
  read, through the secret store's listing; nothing is logged. The audit event carries category,
  reason and count only (`adapter` is in the record's `skipped`, not the event).
- **Failures** are the engine's. Each failed item is logged by exception class only (no message,
  key or value). When every remaining item failed in a pass the step raises `PlatformStepHeldError`
  (counts and class names): retry with backoff, the attempts alert, held in `platform`. A
  `NonRetryableError` from the port is not an item failure: it propagates and the engine writes
  `failed` (the port does not raise one today). Blocking cases: the platform is down; the adapter is not enabled in this process or the tenant has no
  connection data for it (the registry cannot open it); a delete leaves an operation waiting for a
  manual action (`PlatformDeleteIncompleteError`); an `adapter_resources` key of an unknown kind
  (`UnknownResourceKindError`). The operator fixes the cause or acknowledges it with `skip-step`.
- Counts are saved when a pass ends, so after a crash they miss that pass's removals, at most
  `MAX_TRIES_PER_PASS` items (a lower bound).

## The GitHub step

`GithubDeletionStep(port)` (NG6h-6, ADR 0034 step 3, decisions B6 and B9). `GithubOutput` and
`TrackedEnvironment` live in `ports.py`; the worker's `SqlGithubPort` (`worker/github_deletion.py`,
composed by `build_github_port` in `worker/tenant_reconcile.py`) is the real port: it removes a
secret or variable through `GitHubOutputWriter.remove` and an environment through the tenant gateway
(an installation token down-scoped to the repository and permission).

- **The installation is checked first, live** (`GET /app/installations/{id}` with the App JWT, 404 =
  gone; a suspended installation still exists). Gone: the step makes no other GitHub call and no
  write, records `github_outputs` and `github_environments` skipped entries with reason
  `installation_gone` (a count and the repository ids, never a name; the audit event keeps only category, reason and count) and finishes. The rows stay for the database
  step. With nothing recorded there is no call at all. A failing lookup is transient (retry, alert).
- **What it removes** is what the rows record: the secrets and variables in the credential rows'
  `outputs` (the platform step leaves them there, `remove_outputs=False`), then the tracked
  environments. Each removed output is cleared from its row right after the GitHub call (absent on
  GitHub is fine), so a crash repeats an idempotent call and converges.
- **Environments (B6).** Only a row of `github_environments` is ever deleted. GitHub's `created_at`
  is read first: later than the row's plus `CREATED_AT_SKEW` (5 minutes: the row is written after the
  `PUT` from the database clock, so only clock disagreement matters; a margin that is too small keeps
  our own environment, one that is too large deletes a recreated one, so it is no larger than a
  GHES clock drift needs) means someone recreated it: kept, `environment_kept` / `recreated`, row
  forgotten. 404 counts as done. Releasing an environment clears its `environment` outputs from the
  credential rows, then forgets its row (in that order, so a repeat is harmless). The last pass
  records `environment_kept` / `untracked` (a count) for environments the credential rows name that
  no row tracks; those are never deleted. Secrets and variables scoped to a tracked environment are not separate items:
  they are settled with the environment (counted as removed when it is deleted, noted on the
  `recreated` entry when it is kept, never removed by name from a recreated one). A 404 on an
  output's repository clears the entry and records `github_outputs` / `repository_unreachable`
  with the repository ids. A failing installation lookup raises `GithubStepHeldError` (class name only).
- **Pass semantics are the platform step's**: every remaining item (outputs, then environments) is
  tried once per pass, the start rotates by `record.attempts`, progress (cursor `handled`) resets the
  attempts, only a pass where every item failed raises `GithubStepHeldError` (class names and counts
  only; the engine retries, backs off and alerts), the run budget and `LeaseLost` are respected and
  `NonRetryableError` propagates. At most 50 items are tried per pass. Counts are
  `github_outputs_removed` and `github_environments_removed` (a different name from the database
  step's `github_environments`, which counts rows). A crash can lose the counts and the
  `recreated` entry of the pass it interrupted.

## Audit (ADR 0034 "The audit trail", inv. 12, 14)

`tenant.deletion.step` (one per completed step; `counts` are cumulative), `.skipped` (one per skipped
category), `.completed` and `.failed`, actor `reconciler`, `resource_type=tenant_deletion`. Each
definer transition and its events are ONE session transaction through `audit.append`, opened inside
`tenant_context(tenant_id)` (the insert policy admits the context tenant), so `completed` inserts in
the transaction that removed the `tenants` row, and a rolled-back transition leaves no event. The
`deletion` shape allows ids, state and step names, a category, a count and a fixed code; `counts`
go through `deletion_counts`. Free text (`NonRetryableError.message`, an exception message) reaches
only `last_error`, scrubbed and cut, never the audit trail. `tenant.deletion.requested` and the
cancel event are written by the request path (NG6h-7). Leaving `requested` writes a `step` event too (draining is the first destructive step, so every
transition is audited); `counts` on a step event are cumulative.

## Writing step output

`counts` keys (and `skipped` category names) are printable names of at most 64 characters, values
non-negative integers. They must also avoid the admin view's secret words (`has_secret_word` in
`audit/redact.py`; `SECRET_WORDS`: `token`, `password`, `passwd`, `secret`, `authorization`,
`credential`, `key`, matched as substrings, case-insensitively): the admin read drops a key that
contains one, so such a count would vanish from the audit view. Name a category for what it is
without those words (`adapter_resources`, `access_grants`, `parked_values`), not after a table such
as `adapter_credentials`. NG6h-4b, -5 and -6 follow this.

## Settings

The engine's timing is `EngineConfig`; the `DELETION_*` settings (`worker/README.md`) carry the
values and `TrustbridgeSettings` bounds them to what `EngineConfig` and `DrainStep` accept. The
drain poll plus one step pass must fit in half the lease TTL, or the lease can lapse under a running
step (checked at startup).

## Don't

- Write `tenant_deletions` other than through the definer functions.
- Import `worker`, `github`, `adapters`, `webhooks`, `admin`, `rbac`, `signer` or `main` (a
  contract). The adapter and GitHub work of the steps reaches this package as step objects.

## Record retention (B4, NG6h-3c-2)

`DELETION_RETENTION_DAYS` (default **0 = keep for ever**; otherwise at least
`DELETION_RETENTION_MIN_DAYS`, default 90; negative is refused) lets the reconciler's `deletion-purge`
duty (`worker/reconciler.py`, `deletion_record_purge`, every `DELETION_PURGE_INTERVAL_SECONDS`) remove
finished records through the definer `tb_purge_tenant_deletions` (migration `7b2d4f8a1c36`), in at
most `DELETION_PURGE_MAX_BATCHES` batches of `DELETION_PURGE_BATCH_SIZE` per run, stopping after half
the lease TTL. `SqlRecordPurger` (`purge.py`) is the `RecordPurger`; the composition root (NG6h-7)
hands it to `ReconcilerDeps.record_purger`, and with none the duty does nothing.

- `done` records past the cut-off **whose tombstone exists** go, and so do `cancelled` records (no
  tombstone needed: a cancelled deletion left the tenant standing). A `done` record without a
  tombstone is never deleted: it is counted as `unprotected` and logged at WARNING. Open and `failed`
  records are never purged (an operator may still resume or cancel a `failed` one).
- The cut-off is the retention plus a 5-minute margin (`CLOCK_SKEW_MARGIN`), so a retention equal to the
  floor survives a worker clock that leads the database's.
- The tombstones (`tenant_tombstones`) are never purged, so a still-installed App stays refused (B2).
- **Upgrade order.** The previous release reads `done` records and no schema mechanism can say "no
  previous-release pod exists" (`schema_compat` only guards new code on an old schema), so leave the
  retention at 0 until the release is fully rolled out.
- **One-way door.** After a purge the downgrade of `4e9a1c7b5d23` (the tombstone table) refuses,
  because its tombstones have no `done` record behind them. Rolling back past it means restoring from
  a backup.
- **`AUDIT_RETENTION_DAYS=0`.** The two retentions are independent. The `tenant.deletion.*` audit
  events (tenant id, host, installation id; no login) are removed only by the audit purge, so with
  `AUDIT_RETENTION_DAYS=0` they are kept for ever whatever this setting is. See ADR 0034, "Deletion
  records' retention".

## Tracked GitHub environments (NG6h-6a)

`github_environments` (migration `9a4d6c2e8b17`) holds the environments the GitHub writer created itself; the
`database.PLAN` clears it (count `github_environments`) after the GitHub step has deleted them. `ports.TrackedEnvironments`
(`tracked(tenant_id) -> [(repository_id, name)]`, `tracked_since(tenant_id)`, which adds each row's `created_at`, and `forget(tenant_id, repository_id, name)`) is what the GitHub step (NG6h-6) uses: delete the
listed environments, `forget` each once it is gone (or already absent), and record every environment in an output that is
not listed as `environment_kept`. The app role holds `SELECT, INSERT, DELETE` and `UPDATE (created_at)` on it, which is
enough for the step's `FOR UPDATE SKIP LOCKED` batches. `tb_delete_tenant`'s emptiness backstop lists the table too (migration `9a4d6c2e8b17` replaces its
body under an `md5(prosrc)` guard, before and after the delete).
