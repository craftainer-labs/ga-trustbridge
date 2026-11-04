# 0032. Append-only audit log with redaction, export and retention

## Status

Accepted (2026-11-04). The user's G-api answers (2026-11-03) are recorded below. The implementation
merged: the table, grants, triggers and purge path in NG6c, the emitters in NG6f. Builds on
[0028](0028-trustbridge-architecture-and-instance-layout.md) (database roles, source address
behind `TRUSTED_PROXIES`, N-1 migrations) and
[0031](0031-tenancy-and-row-level-security.md) (tenant context and RLS), and is used by
[0033](0033-admin-api-v1-rbac-and-limits.md) (which actions are audited and who may read).

## Context

EB-4 requires an append-only audit log (who, what, when, source IP, before/after) for authn,
authz, configuration and credential events, with a JSON export for a SIEM. EB-1 requires users
to be keyed by a durable IdP id, never handle or email. EB-15 asks for retention settings.
What the code base gives us:

1. **Identity.** The template validates a bearer token and exposes its claims
   (`app/oidc.py`, `get_current_claims`); `sub` is the only claim NFR-0014 allows us to assume
   is present. Rejections are logged as a WARNING with path and status only, since a rejected
   token has no verified subject (`get_current_claims`). The template's own audit hook
   (`craftainer_crud.controllers.crud_actions`) names the caller from `request.state.claims`.
2. **What the API sees.** The admin API is a resource server: it sees bearer tokens only. Login,
   logout, MFA and password events happen at the IdP and the BFF (ADR 0026), not here. "Authn
   events" for this API are therefore token rejections, and successful use of a token.
3. **Source address.** `request.client.host`, rewritten by uvicorn's `ProxyHeadersMiddleware`
   for `TRUSTED_PROXIES` (0028 "Source address behind a proxy").
4. **Tenant.** Most events belong to a tenant (0031); some do not (a host registration, an
   instance-level role denial, the audit purge itself), so `tenant_id` is nullable.
5. **Roles.** The runtime role cannot do DDL; the owner role runs migrations (0028). No
   `BYPASSRLS` role exists (0031).
6. **Secrets.** Platform credentials and webhook secrets live in the secret store, and
   adapter connection data is operator-only (NG5f, NG5d; 0033). Several audited resources
   nonetheless carry secret-adjacent fields (connection `base_url`, credential names,
   operation state).

## Decision

### Event model

One table, `audit_events`, one row per event, written by trustbridge code only:

| Column | Meaning |
| --- | --- |
| `id` | UUID, the event's public identifier |
| `seq` | `bigint GENERATED ALWAYS AS IDENTITY`; the export cursor (see "Export") |
| `occurred_at` | `timestamptz`, set by the database (`now()`, the transaction start), never by the caller |
| `recorded_at` | `timestamptz DEFAULT clock_timestamp()`, the insert time; the export's lag filter uses it |
| `tenant_id` | nullable UUID; no foreign key, so events outlive a tenant row |
| `actor_type` | `user` or `system` |
| `actor_sub`, `actor_iss` | for `user`: the token's `sub` and `iss`, the durable id. Never email, handle or display name (EB-1). For `system`: a fixed name (`worker`, `reconciler`, `github-webhook`, `migrate`) and `actor_iss` null |
| `on_behalf_of` | nullable; for a webhook-driven event the GitHub sender's numeric `id` and the delivery id, never the login |
| `source_ip` | `inet`, from `request.client.host`; null for system events. Never read from `X-Forwarded-For` directly (0028) |
| `action` | dotted verb, from a closed enum in code: `tenant.disable`, `credential.rotate`, `delivery.requeue`, `host.registration.start`, `authz.denied`, ... (there is no `authn.rejected` action: unauthenticated rejections are never rows, user decision 1) |
| `resource_type`, `resource_id` | what was acted on; ids, not names that can change |
| `outcome` | `success`, `denied` or `failure` |
| `before`, `after` | `jsonb`, redacted (below); null when not applicable |
| `request_id`, `trace_id` | correlation with logs and traces (ADR 0021) |
| `schema_version` | integer version of the event shape |

**What is audited** (the emitters are NG6f; the list is the contract):

- Every mutating admin API call, success, failure and denial, with before/after.
- Every authorization denial of an authenticated principal (`authz.denied`: role missing,
  tenant not granted, including a request answered 404 for a tenant the principal has no
  grant on). A denial is written in its own transaction with **a null `tenant_id`** (no
  tenant context exists at that point and the insert policy permits a null tenant), and the
  requested tenant id in `resource_id` with `resource_type = tenant`; only instance-scoped
  auditors see denials.
- Credential events: issue, rotate begin and finish, revoke, abandoned operation, orphan
  deleted by the sweep, and each OIDC trust created or removed. Metadata only; never a value.
- Configuration events: tenant state changes by installation events (`_sync`), host
  registration, adapter configuration changes made through the API, role-mapping changes if
  mappings live in the database (none: roles live in Keycloak, 0033).
- The audit purge itself (a summary event with the cut-off and row count) and each export
  request (who exported, the range).
- Authn failures: rejected, unauthenticated requests are logs and metrics only; authorization
  denials are rows (user decision 1 below).

**Same transaction.** For a mutating API call and for a credential state change, the audit row
is inserted in the transaction that makes the change, so there is no change without its
event and no event for a rolled-back change. Denials, and failures that roll back, are written
in their own short transaction. Audit writes use the same tenant context as the action (0031).

### Redaction

`before` and `after` are built by an **allow-list per resource type** in code (a function from
the model to the fields that may be recorded), never by dumping a row and removing names. A
new column is therefore absent from the audit until someone adds it. Secrets never appear:
connection data is recorded as `{"configured": true}`, credentials as their id, adapter and
state (and rotation timestamps), webhook bodies not at all. A free-text field (an error
message) is recorded only through the same redaction used by the logs (ADR 0007), and a value
longer than a fixed bound is truncated with a marker. A test plants a canary secret in every
audited input and asserts it appears in no row (invariant 6).

### Append-only enforcement

Three layers, from the database outward:

1. **Grants.** `trustbridge_app` has `SELECT` and `INSERT` on `audit_events` and nothing else:
   no `UPDATE`, `DELETE` or `TRUNCATE`. The migration `GRANT`s only (0031: roles are the DBA's
   or Helm's).
2. **Trigger.** `BEFORE UPDATE OR DELETE` row triggers and a `BEFORE TRUNCATE` statement
   trigger raise unless `current_user` is `trustbridge_owner`. This catches a later grant
   mistake and a bug in the app role's own code path.
3. **Purge through one function.** Retention deletes rows only through
   `tb_audit_purge(before timestamptz)`, a `SECURITY DEFINER` function owned by the owner
   role with a fixed `search_path`, which refuses a cut-off newer than the configured
   retention floor and writes its own summary event. It is the only path that satisfies the
   trigger.

RLS (0031) is enabled and forced on `audit_events` from creation (a new table, so no previous
release touches it; 0031 lists it as a seventh RLS table). Its policies: an `owner_all` policy
for `trustbridge_owner` (the definer functions `tb_audit_purge` and `tb_audit_list` run as the
owner and need it, and the append-only trigger still gates the owner's changes), and for the
app role `INSERT` with `tenant_id` equal to the context tenant or
null; `SELECT` of rows whose `tenant_id` equals the context tenant. Reading events with a null
`tenant_id`, or across tenants (instance-scoped auditors), goes through
`tb_audit_list(filters...)`, a `SECURITY DEFINER` function with the same shape and the same
caveat as 0031's functions; the API calls it only for an instance-scoped role.

**No `RETURNING`.** PostgreSQL applies the `SELECT` policy to the rows an `INSERT ... RETURNING`
returns, and a null-tenant row (an `authz.denied`) is not selectable by the app role. Audit
inserts therefore supply the `id` (a UUID generated in code) and never use `RETURNING`
(`seq` and the defaults are not read back); an invariant tests a null-tenant insert.

### Read scope

Reads are served by the admin API (0033). Only the `auditor` role reads audit (0033: an `admin` or
`operator` without `auditor` gets 403; separation of duties). A tenant-scoped `auditor` reads
that tenant's events through the tenant-scoped routes `GET /admin/v1/tenants/{t}/audit-events`
and `.../export`, which set the tenant context; an instance-scoped `auditor` reads across
tenants, including events with a null tenant, through `GET /admin/v1/audit-events` and its
export, which call `tb_audit_list`. An auditor's reads are themselves not audited per row, but an export request is
(above). No role can write, edit or delete an event through the API; there is no such endpoint.

### Export

The transport is user decision 2 below. The export is:

- **Ordered and resumable** by `seq`, one JSON object per event with the columns above.
- **Lag-bounded.** Because identity values can commit out of order (a transaction with a lower
  `seq` can become visible after one with a higher `seq`), an export returns only events whose
  `recorded_at` is older than `AUDIT_EXPORT_LAG_SECONDS` (default 30), so a SIEM that resumes
  from the last `seq` does not skip a late commit. The guarantee holds only if no
  audit-writing transaction stays open longer than the lag, so the admin API, the worker and
  the deletion batches run with a `transaction_timeout` (or `statement_timeout` plus
  `idle_in_transaction_session_timeout`) below the lag, and the lag is validated at startup
  against that timeout. This is a stated consistency model, not real-time delivery.
- **Filtered by scope** (the reader's tenants) and by a time or `seq` range.
- Itself audited.

### Retention

`AUDIT_RETENTION_DAYS` (proposed default 365; `0` means never purge). The reconciler runs the
purge as a singleton duty (EB-6) through `tb_audit_purge`, in bounded batches so it never
holds a long lock. The function refuses a cut-off inside a configured floor
(`AUDIT_RETENTION_MIN_DAYS`, default 90) so a typo cannot erase recent history. The retention
interacts with EB-15 deletion ([0034](0034-tenant-data-deletion.md)): the deletion's own
events always survive, and 0034 holds the open decision on the rest of a deleted tenant's
rows.

### Migration

Part of the audit work (NG6c): one expand migration creates the table, the indexes
(`(tenant_id, seq)`, `(occurred_at)`, `(actor_sub, seq)`), the grants, the triggers, the two
functions and the policies; the application writes to it once the migration has run, and the
previous release, which never touches the table, is unaffected (NFR-0027). Because the table
is new, the migration writes the existing `schema_compat` without a bump.

## User decisions (G-api, 2026-11-03)

These replace the open options the first draft carried. Alternatives considered are kept in
one line each so the reasoning survives.

1. **Authentication failures: logs and metrics for unauthenticated rejections; rows for
   `authz.denied`.** **User decision (G-api, 2026-11-03).** A rejected token has no verified
   subject and an anonymous caller controls how many there are, so it must not create rows
   (considered and not chosen: a rate-limited row per rejection; a sampled row per source and
   minute). A denial of an authenticated principal has a verified `sub`, so it is stored.
   Rejections are a structured log line with a fixed schema and a counter by reason.
2. **Export: NDJSON pull plus the JSON list endpoint.** **User decision (G-api,
   2026-11-03).** `GET /admin/v1/audit-events/export?since_seq=...` streams
   `application/x-ndjson`, resumable by `seq`, for SIEMs and air-gapped installs (no outbound
   call, EB-5); the list endpoint (cursor pagination, 0033) serves the UI's viewer. **Push to a
   SIEM (syslog, webhook, OTLP) is a follow-up**, not part of NG6.
3. **Tamper evidence: grants, trigger and purge only through the definer function; no hash
   chain.** **User decision (G-api, 2026-11-03).** The SIEM copy is the evidence an auditor
   trusts. (Considered and not chosen: a per-tenant hash chain, which serialises inserts on the
   chain head; revisit if a SOC 2 scope requires in-database tamper evidence.)

Sources: EB-4; EB-5; OWASP Logging Cheat Sheet; NDJSON; NIST SP 800-92 (see Sources).

## Decision needed

None open in this ADR. The question of what happens to a deleted tenant's audit rows is a
decision in [0034](0034-tenant-data-deletion.md), because it interacts with deletion.

## Invariants the implementation must test

Each is one testable sentence; NG6c and NG6f implement and test them against a real Postgres
with the app role.

**Append-only**

1. As `trustbridge_app`, `UPDATE`, `DELETE` and `TRUNCATE` on `audit_events` fail, and an
   `INSERT` and a `SELECT` of the context tenant's rows succeed.
2. With a grant mistake simulated (the app role granted `UPDATE`), the trigger still rejects the
   statement; only `current_user = trustbridge_owner` passes it.
3. `tb_audit_purge` deletes only rows older than its cut-off, refuses a cut-off inside
   `AUDIT_RETENTION_MIN_DAYS`, never runs for another role to delete directly, and writes its
   own summary event.
3a. An `authz.denied` row with a null `tenant_id` is inserted by the app role in a context-less
   transaction without error (no `RETURNING`).
4. `occurred_at` is assigned by the database: a caller-supplied value is ignored or rejected.

**Content**

5. An event for an authenticated user records the token's `sub` and `iss`, and no email, login
   or display name appears in any column, including `before` and `after`.
6. A canary secret placed in connection data, a credential value, a webhook body and an error
   message appears in no `audit_events` row; `before` and `after` contain only allow-listed
   fields of the resource type, and a column added to a model without an allow-list entry
   does not appear.
7. `source_ip` is the ASGI client address after `ProxyHeadersMiddleware`; a spoofed
   `X-Forwarded-For` from an untrusted peer does not change it.
8. Every mutating admin endpoint writes exactly one event with `action`, `resource_type`,
   `resource_id`, `outcome` and, for changes, `before`/`after`; a test enumerates the router's
   mutating routes and fails for one with no emitter.
9. A mutating call's event commits with its change, and a change that rolls back leaves no
   `success` event (it may leave a `failure` event in its own transaction).
10. An `authz.denied` event, including one for a tenant answered 404, carries the denied
    principal's `sub` and the missing role or the requested tenant id (in `resource_id`, with a
    null `tenant_id`), and an unauthenticated rejection creates a log line and metric but, no row.

**Scope and export**

11. A tenant-scoped `auditor` sees only its tenant's events through the tenant-scoped routes
    (including the export), never events of another tenant or with a null tenant; an
    instance-scoped `auditor` sees all; a principal with `admin` or `operator` but not
    `auditor` gets 403 on every audit route.
12. The export is ordered by `seq`, resumable from the last `seq` without gaps or repeats, and
    returns no event whose `recorded_at` is newer than `AUDIT_EXPORT_LAG_SECONDS`, including
    when a long-running transaction commits late (and a transaction longer than the lag is
    aborted by the timeout).
13. An export request writes its own event naming the reader and the range.
14. No API route writes, updates or deletes an audit event.

**Retention and migration**

15. With `AUDIT_RETENTION_DAYS=0` nothing is purged; otherwise the purge removes only rows past
    retention, in batches, and runs on one reconciler at a time.
16. The audit migration passes the `migration-compat` job and the previous release's suite
    against the new table.

## Open issues

- **Volume.** A busy instance writes one row per mutating call and per credential event;
  partitioning `audit_events` by month (so a purge is a partition drop) is deferred until a
  size is measured. It needs an expand migration and a change to the purge function only.
- **Clock.** `occurred_at` and `recorded_at` are the database clock; skew between replicas is irrelevant, but
  the SIEM correlation with log timestamps from pods is by `request_id`, not time.
- **GitHub-originated actors.** A webhook-driven change records the GitHub sender's numeric
  id in `on_behalf_of`; mapping it to a person is the SIEM's or the customer's job.
- **Event versioning.** `schema_version` lets the shape evolve; the export documents it in the
  OpenAPI schema (0033) and treats additive changes as compatible.
- **Reads of audit data** are not logged per row (only exports). If an auditor-of-auditors
  requirement appears, per-read events would be added.

## Consequences

- The audit trail is enforced in the database, not by convention: a bug in application code
  cannot rewrite history through the app role.
- Same-transaction writes mean an audit insert failure fails the action it records, which is
  intended for a compliance log and costs availability if the table is unwritable.
- An allow-list per resource type is more code than a generic dump and a reviewer must keep it
  current, but a new secret-bearing column is safe by default.
- Owner-level access (a DBA) can still change the table; that is outside the threat model of
  this design and is what the SIEM copy covers (user decision 3).
- The export's lag makes it near-real-time, not real-time; the lag is a setting.
- Audit rows with a tenant id outlive the tenant's data (EB-15 interplay above) until
  retention expires.

## Sources

- EB-1, EB-4, EB-15 (the plan "Enterprise baseline" rows); GitHub App best practices:
  https://docs.github.com/en/apps/creating-github-apps/about-creating-github-apps/best-practices-for-creating-a-github-app
- PostgreSQL privileges, triggers (`BEFORE TRUNCATE`), row security and identity columns:
  https://www.postgresql.org/docs/current/ddl-priv.html,
  https://www.postgresql.org/docs/current/sql-createtrigger.html,
  https://www.postgresql.org/docs/current/ddl-rowsecurity.html,
  https://www.postgresql.org/docs/current/ddl-identity-columns.html
- OWASP Logging Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html
- NIST SP 800-92, Guide to Computer Security Log Management:
  https://csrc.nist.gov/pubs/sp/800/92/final
- NDJSON: https://github.com/ndjson/ndjson-spec
- ADRs [0028](0028-trustbridge-architecture-and-instance-layout.md),
  [0031](0031-tenancy-and-row-level-security.md), 0007 (structured logging), 0021 (traces),
  0026 (resource server and BFF); NFR-0014, NFR-0027.
- Code read: `src/app/oidc.py` (`get_current_claims`, `require_roles`),
  `src/app/controllers/audit.py`.
