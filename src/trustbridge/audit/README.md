# trustbridge/audit/

Storage layer of the append-only audit log ([ADR 0032](../../../docs/adrs/0032-audit-log.md),
Proposed until the emitters, NG6f, merge). Out of scope here: the emitters at call sites (NG6f),
the admin API routes serving list and export (NG6d) and tenant deletion (NG6h). SIEM push is a
follow-up.

- `model.py`: `AuditEvent` (what to write; validates actor, action and ip shape), `AuditRecord`
  (what is read; `to_json` and `to_ndjson_line` are the export shape), `AuditQuery` (filters and
  the `seq` cursor), `AuditPage`.
- `store.py`: the functions below.

## API

All take an `AsyncSession` (or factory) and leave the transaction to the caller, except
`purge_expired`.

| Function | Use |
| --- | --- |
| `append(session, event) -> UUID` | Insert in the caller's transaction, so the row commits with the change. No `RETURNING` (a null-tenant row is not selectable by the app role); the id is generated in code. `tenant_id` must be null or the session's tenant context. |
| `list_events(session, query, *, tenant_id=None, instance_scope=False) -> AuditPage` | One page. `tenant_id` set: that tenant (open the session in its `tenant_session`; the tenant is also filtered explicitly). `instance_scope=True` with `tenant_id=None`: instance scope through `tb_audit_list()`, all tenants and null-tenant events; the caller must have checked the instance-scoped `auditor` role, this layer does not. `tenant_id=None` without the flag, or both together, raises `ValueError` before any query, so a failed tenant lookup never widens a read. NG6d passes the flag only after the role check. |
| `export_ndjson(session, query, *, tenant_id=None, instance_scope=False, lag_seconds, page_size) -> AsyncIterator[bytes]` | Ascending `seq`, one line per event, resumable with `AuditQuery.after_seq`. Same scope rules as `list_events`. |
| `purge_batch(session, *, before, min_days, batch_size)` | One `tb_audit_purge` call; returns rows deleted. |
| `purge_expired(factory, *, retention_days, min_days, batch_size, now)` | Loops `purge_batch`, one short transaction per batch; `retention_days=0` never purges. For the reconciler's singleton duty. |

**Cursor.** `AuditPage.next_seq` is the last item's `seq` when more follow, else `None`. Continue
an ascending read with `AuditQuery(after_seq=next_seq)`, a descending one
(`descending=True`) with `before_seq=next_seq`. NG6d wraps `seq` in an opaque token if it wants.
`limit` is 1 to 1000.

**Export lag.** `lag_seconds` makes a read stop before the first event whose `recorded_at` is
newer than the lag (not merely skip such events), so a consumer resuming from the last `seq`
never skips a late commit. It is a consistency model, not real time; writers must run with a
transaction timeout below the lag (settings validation of that is NG6f/NG6d's, at startup).

**NDJSON shape.** One compact JSON object per line, `\n`-terminated, keys in column order:
`id, seq, occurred_at, recorded_at, tenant_id, actor_type, actor_sub, actor_iss, on_behalf_of,
source_ip, action, resource_type, resource_id, outcome, before, after, request_id, trace_id,
schema_version`. Timestamps are UTC with a `Z`; ids and `source_ip` are strings. The route sets
`application/x-ndjson` and writes the export's own audit event.

## Database side (migration `e5c9a37d2b18`)

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

## Settings

`AUDIT_RETENTION_DAYS` (365; `0` never purges; otherwise at least the floor),
`AUDIT_RETENTION_MIN_DAYS` (90), `AUDIT_EXPORT_LAG_SECONDS` (30).

## Rules

- Validating `before`/`after` serializability, `outcome` and `schema_version` at construction is
  NG6f's (it owns the action enum and redaction); until then a bad value fails inside the caller's
  transaction at `append`.
- `AUDIT_RETENTION_DAYS` equal to `AUDIT_RETENTION_MIN_DAYS` passes validation but leaves no
  clock-skew margin: an app clock ahead of the database's makes every purge raise 22023. Keep
  retention above the floor by a margin. `AUDIT_EXPORT_LAG_SECONDS` has no upper bound.
- Redaction is the emitter's job (allow-list per resource type, NG6f); this layer stores
  `before` and `after` as given.
- Never `RETURNING` on an insert; never write `occurred_at`; never delete except via the purge.
