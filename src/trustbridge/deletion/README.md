# trustbridge/deletion/

The tenant deletion engine ([ADR 0034](../../../docs/adrs/0034-tenant-data-deletion.md), NG6h-4a-2):
the state machine, the lease and fence loop, the failure policy, the drain step and the audit
events. The database half is migration `5d7a2c8e1b94` (`tenancy/README.md`). The reconciler duty
(`worker/reconciler.py`, `tenant_deletion`), the `DELETION_*` settings, the B10 gate
(`tenancy/gate.py`) and the [runbook](../../../docs/runbooks/tenant-deletion.md) are NG6h-4a-3; the
three steps are filled by NG6h-4b (database), -5 (platform) and -6 (GitHub). Until then the steps are
Protocols and the tests use doubles, and nothing composes a `DeletionEngine` in a process: the
reconciler runs the duty only when it is given a `DeletionDriver` (`ReconcilerDeps.deletion`,
`ProcessOverrides.deletion`).

The duty is inert in production until the steps exist: no deletion can run with a step missing. The composition
root that builds the `DeletionEngine` with the real steps and hands it to the reconciler lives outside the worker
package and comes in NG6h-7, once 4b, 5 and 6 have landed.

- `ports.py`: `DeletionDriver` (`run_once() -> bool`). The only module the worker and reconciler
  may import (import-linter contract 3, which fails if that import ever disappears); it imports
  nothing from the package, and `__init__` re-exports nothing, so depending on it loads no engine
  code.
- `engine.py`: `DeletionEngine(store, EngineSteps, EngineConfig)`, the `DeletionDriver`.
- `model.py`: `DeletionRecord`, `Lease`, `StepContext`, `StepOutcome`, and the control-flow
  exceptions `LeaseLost`, `RunBudgetExhausted` and `NonRetryableError`.
- `steps.py`: the `Step` Protocol and the slots `PlatformStep`, `GithubStep`, `DatabaseStep`, plus
  `WritersPort` for the drain.
- `drain.py`: `DrainStep`.
- `database.py`: `SqlDatabaseStep`, the `DatabaseStep` (NG6h-4b). See "The database step" below.
- `store.py` / `sql.py`: `DeletionStore` and `SqlDeletionStore` over the definer functions.
- `audit.py`: the event builders. `metrics.py`: `trustbridge.deletion.attempts` and `.failed`.

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
`adapter_credentials`, `adapter_resources`, `scope_leases`, `tenant_repositories`, then clears `webhook_deliveries`
bodies), never the `tenants` row. One pass is one batch of one table in one transaction inside
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
  `tenant_repositories` and `webhook_bodies`: the tables `credential_operations` and `adapter_credentials` contain a
  secret word, so their counts take other names. A new tenant-keyed table is added to `database.PLAN`; an integration
  test fails when a table with a `tenant_id` column is in neither `PLAN` nor the retained set.
- The batch size is a constructor argument; the composition root (NG6h-7) chooses it.

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
