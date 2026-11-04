# trustbridge/audit/

The append-only audit log ([ADR 0032](../../../docs/adrs/0032-audit-log.md), Proposed until the
admin API reads and the mutating endpoints are in): the storage layer (NG6c), event value
validation, redaction and the emitters at the call sites that exist today (NG6f). Out of scope
here: the admin API routes serving list and export (NG6d), the mutating endpoints' events (NG6e)
and tenant deletion (NG6h). SIEM push is a follow-up.

- `model.py`: `AuditEvent` (what to write; validates every value at construction), `AuditRecord`
  (what is read; `to_json` and `to_ndjson_line` are the export shape), `AuditQuery` (filters and
  the `seq` cursor), `AuditPage`.
- `actions.py`: `Action`, the closed set of actions, and `SystemActor`, the fixed system names.
- `redact.py`: `project` (allow-list per shape), `scrub_text`, `truncate`.
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
| `tenant.sync` | `github.handlers.InstallationHandlers` (`created`, `deleted`, `suspend`, `unsuspend`) through `TenantSyncAudit`, wired in `worker.runtime.build_core` | `github-webhook`, `on_behalf_of` = `{"github_sender_id": <numeric id>, "delivery_id": "<id>"}` (JSON object; null, not an empty object, when the payload has no numeric sender) | Own, after the tenant change and the repository changes. A failure fails the handler, the worker retries, and the sync repeats (a retry may add a second event; a change is never left without one). `after` = `{"state"}`; `before` is not recorded (the sync overwrites the state from GitHub's current view). |
| `authz.denied` | `rbac.deps.require_role` through `RbacRuntime.denials` (`AuditDenials`), for a role that is missing and for a tenant that is unknown, malformed or not granted (answered 404) | the denied user (`sub`, `iss`), `source_ip` from the ASGI client, `request_id` from a plain `X-Request-ID` | Own, null `tenant_id`. Instance route: `resource_type=role`, `resource_id=<required role>`. Tenant route: `resource_type=tenant`, `resource_id=<requested tenant id>` (none if the path value is not a canonical UUID). If the sink fails the request fails. Unauthenticated rejections are never rows. |
| `credential.rotate.begin`, `credential.rotate.finish`, `credential.issue`, `credential.revoke`, `credential.operation.abandoned`, `oidc_trust.create`, `oidc_trust.remove` | `adapters.sql_store.SqlCredentialStore.save` through its `observer` (`credentials.CredentialAuditObserver`) | `worker` or `reconciler` (chosen at construction) | The save's own, so the event commits with the credential state. See `credentials.py` for the transition table. |
| `credential.orphan.delete` | `adapters.core.CredentialCore.sweep_orphans` through `CoreDeps.orphans` | as above | Own; one event per platform credential deleted. If the write fails the sweep still finishes, logs each unaudited orphan at error level, then raises. `resource_id` is the platform name. |
| `audit.purge` | written by `tb_audit_purge` itself (actor `reconciler`) | | The purge's. |

`CredentialCore` is not instantiated by any process yet (NG5/NG6), so the credential events are
wired by whoever builds it: `SqlCredentialStore(session_factory, CredentialAuditObserver(writer,
actor))` and `CoreDeps(..., orphans=observer)`. The api wires `RbacRuntime(config, access,
AuditDenials(AuditWriter(session_factory)))` when it mounts the admin API (NG6d).

## Deferred events

ADR 0032 lists events whose flows do not exist on main yet. Their `Action` members exist where the
ADR names them, with no emitter:

| Event | Owner |
| --- | --- |
| Every mutating admin API call with before/after (`tenant.disable`, `delivery.requeue`, `credential.rotate`, ...) and the router-walk test (inv. 8) | NG6e |
| `host.registration.start` and adapter configuration changes through the API | NG6e |
| `audit.export` (who exported, the range) and the audit read routes | NG6d |
| The deletion's own events (0034) | NG6h |
| Wiring `AuditDenials` into the mounted router | NG6d |
| A `transaction_timeout` (or statement and idle-in-transaction timeouts) on the api, worker and deletion connections, and the startup check of `AUDIT_EXPORT_LAG_SECONDS` against it (ADR 0032 "Export"). No such timeout exists on main and the engine is the template's (`app.db`) | NG6d (api) with NG6h (deletion batches) |

An operation ending `failed` (rolled back, previous state restored) has no event of its own
because ADR 0032 does not list it; the inverse `oidc_trust` event shows a rolled-back kind change.

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
string value is kept as a JSON string). It only alters the column: no roles, no grants.

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

- `AUDIT_RETENTION_DAYS` equal to `AUDIT_RETENTION_MIN_DAYS` passes validation but leaves no
  clock-skew margin: an app clock ahead of the database's makes every purge raise 22023. Keep
  retention above the floor by a margin. `AUDIT_EXPORT_LAG_SECONDS` has no upper bound.
- Redaction is the emitter's job (`redact.project`); `append` stores `before` and `after` as
  given, after the construction-time shape and size checks.
- Never `RETURNING` on an insert; never write `occurred_at`; never delete except via the purge.
