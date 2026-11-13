# trustbridge/deletion/

The tenant deletion engine ([ADR 0034](../../../docs/adrs/0034-tenant-data-deletion.md), NG6h-4a-2):
the state machine, the lease and fence loop, the failure policy, the drain step and the audit
events. The database half is migration `5d7a2c8e1b94` (`tenancy/README.md`). The reconciler duty,
settings, the B10 gate and the runbook are NG6h-4a-3; the three steps are filled by NG6h-4b
(database), -5 (platform) and -6 (GitHub). Until then the steps are Protocols and the tests use
doubles.

- `ports.py`: `DeletionDriver` (`run_once() -> bool`). The only module the worker and reconciler
  may import (import-linter); it imports nothing from the package, and `__init__` re-exports
  nothing, so depending on it loads no engine code.
- `engine.py`: `DeletionEngine(store, EngineSteps, EngineConfig)`, the `DeletionDriver`.
- `model.py`: `DeletionRecord`, `Lease`, `StepContext`, `StepOutcome`, and the control-flow
  exceptions `LeaseLost`, `RunBudgetExhausted` and `NonRetryableError`.
- `steps.py`: the `Step` Protocol and the slots `PlatformStep`, `GithubStep`, `DatabaseStep`, plus
  `WritersPort` for the drain.
- `drain.py`: `DrainStep`.
- `store.py` / `sql.py`: `DeletionStore` and `SqlDeletionStore` over the definer functions.
- `audit.py`: the event builders. `metrics.py`: `trustbridge.deletion.attempts` and `.failed`.

## One run

`run_once()` claims one runnable deletion (`tb_claim_tenant_deletion`: lease free or expired,
`not_before` and `retry_at` passed, fence bumped) and walks it `requested > draining > platform >
github > database > done`. Before every pass it renews the lease. A step returns a `StepOutcome`:
`counts` are added to the record's, `skipped` entries (`category`, `reason`, `count`) are appended,
and `done=False` with a `cursor` saves progress in the same state and runs the step again.

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
  lease) or by an operator (`tb_operator_fail`, NG6h-7). A blocked step holds the deletion; it
  never advances on its own. An error recording the retry or the failure itself (the database
  down) propagates from `run_once`; the lease then lapses and the deletion is claimed again.
- **Draining.** `DrainStep` calls `WritersPort.quiesce(tenant_id)` (re-scan `webhook_deliveries`
  recorded for the tenant and its `credential_operations`, abandoning in-flight ones) until two
  consecutive passes find nothing, every `poll_seconds`, at most `max_passes` passes
  (`DrainNotQuiescent`, transient, otherwise). There is no writer-barrier column: the `deleting`
  refusals stop new writes and `tb_delete_tenant` holds the installation's advisory lock.
- **Finish.** After the `database` step the engine calls `DeletionStore.finish`: in one
  transaction `tb_delete_tenant(tenant, holder, fence)` (the `tenants` row, last), then
  `tb_finish_tenant_deletion` (`done`, the tombstone) and the audit events. The `DatabaseStep`
  therefore clears everything except the `tenants` row.

## Audit (ADR 0034 "The audit trail", inv. 12, 14)

`tenant.deletion.step` (one per completed step; `counts` are cumulative), `.skipped` (one per skipped
category), `.completed` and `.failed`, actor `reconciler`, `resource_type=tenant_deletion`. Each
definer transition and its events are ONE session transaction through `audit.append`, opened inside
`tenant_context(tenant_id)` (the insert policy admits the context tenant), so `completed` inserts in
the transaction that removed the `tenants` row, and a rolled-back transition leaves no event. The
`deletion` shape allows ids, state and step names, a category, a count and a fixed code; `counts`
go through `deletion_counts`. Free text (`NonRetryableError.message`, an exception message) reaches
only `last_error`, scrubbed and cut, never the audit trail. `tenant.deletion.requested` and the
cancel event are written by the request path (NG6h-7). Leaving `requested` writes no step event.

## Don't

- Write `tenant_deletions` other than through the definer functions.
- Import `worker`, `github`, `adapters`, `webhooks`, `admin`, `rbac`, `signer` or `main` (a
  contract). The adapter and GitHub work of the steps reaches this package as step objects.
