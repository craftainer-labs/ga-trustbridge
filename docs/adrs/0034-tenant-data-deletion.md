# 0034. Tenant data deletion (EB-15): a write-ahead, resumable operation

## Status

Proposed. The user decided on 2026-11-03 (G-api) that tenant data deletion is **in NG6**,
overriding the earlier lean to defer it; this ADR holds its design, with three decisions still
open for the user ("Decision needed"). No code is written yet. It becomes Accepted when the
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
  `state` (`requested`, `draining`, `platform`, `github`, `database`, `done`, `failed`),
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

1. The tenant is **admin-disabled** (0033), so no new adapter operation or handler runs.
2. The request body **echoes the tenant's host, installation id and organisation login**
   (a typed confirmation of an irreversible action) and carries a reason (recorded in the
   audit).
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
   deleted is "Decision needed 1".**
3. **GitHub.** Remove the secrets, variables and environments trustbridge wrote, as tracked in
   the tenant's resource and credential rows, using an installation token, per repository. If
   the installation no longer exists (the App was uninstalled), GitHub has already revoked
   access and the outputs cannot be removed from our side: they are recorded in `skipped` as
   `installation_gone`, with repository ids, so the customer can remove them. Nothing outside
   the tracked outputs is touched.
4. **Secret store.** Remove the tenant's parked values and its per-tenant Argo CD webhook token.
   **The adapter connection data (`store:tenants/<uuid>/<adapter>`) is not deleted by the API**,
   consistent with it being operator-only (0033): the record lists the keys under `skipped` as
   `operator_owned`, for the operator to remove. (A judgement call, listed so the user can
   overrule: it keeps the API from gaining any write path to that store.)
5. **Database.** In the tenant's context (0031), in batches and in the order
   `credential_operations`, `adapter_credentials`, `adapter_resources`, `scope_leases`,
   `tenant_repositories`, then the `tenants` row last, each batch its own transaction and
   counted. Webhook bodies are cleared for deliveries recorded for the tenant: an expand
   migration adds a nullable `tenant_id` to `webhook_deliveries`, set by the worker when it
   resolves the delivery; deletion nulls `body` (keeping the row for deduplication, as the
   retention purge does, 0028 item 5). Rows with no `tenant_id` (never resolved, or older than
   the column) fall to the normal retention and are counted under `skipped` as
   `retention_only`.
6. **Done.** The record's `state` becomes `done` with its counts. The audit trail of the
   deletion is written throughout (below).

A step that fails records the error and is retried with backoff; a **blocked step** (for example
a platform that stays unreachable) holds the deletion in that state until the operator decides
(**Decision needed 2**). The reconciler resumes non-terminal deletions whose lease is free, as a
singleton duty (EB-6). There is no cancel after the first destructive step; before it (state
`requested`) an admin may cancel.

### The audit trail of the deletion

Events (0032) are written by the deletion itself: `tenant.deletion.requested` (who, reason, the
echoed identifiers, `before` = the tenant's non-secret summary), one `tenant.deletion.step`
per completed step with counts, `tenant.deletion.skipped` per skipped category, and
`tenant.deletion.completed`. They carry the tenant id, are inserted in the same transaction as
the step they record, and **survive the deletion**: `audit_events` has no foreign key to the
tenant and the database step never touches it. The reconciler's audit purge (0032) removes them
only at retention. What happens to the **other** audit rows of the tenant is **Decision needed
3**.

### After deletion

The tenant id no longer resolves. If the GitHub installation still exists, its next event
creates a new tenant with a new id (`tb_upsert_tenant`); the response to the deletion request
and its record state this, and recommend uninstalling the App first. Re-creation is not
prevented (see Open issues).

## Decision needed

For each item: the options, their trade-offs, the sources, and a lean that is only a lean.

### 1. What the platform step deletes

- **A. Credentials and trust only.** Revoke tokens and robot accounts, remove OIDC identity
  mappings and webhook policies, remove repository credentials: everything that grants access
  or that CI uses. Projects, repositories and artifacts on the platform stay. The tenant's data
  in the platform is not destroyed, and a wrong click cannot delete artifacts; cost: the
  platform keeps projects that trustbridge created, which the customer must remove.
- **B. Everything trustbridge provisioned, including projects and repositories.** A complete
  removal, which is what "deletion" can mean for a compliance request; cost: irreversible loss
  of artifacts and images stored there (Harbor project with images, Docker Hub repository),
  and adapters differ in whether they can delete a non-empty project at all.
- **C. A per-request flag**: A by default, B when the request sets `delete_platform_resources`
  (and the confirmation echoes it), listed per adapter in the record.

Lean: **C**, defaulting to A. Credentials and trust are what trustbridge is responsible for;
deleting stored artifacts is a different order of harm and should be an explicit, per-request
choice. Sources: EB-15; 0029 adapter delete semantics (the adapter contract says what each
adapter can delete); NIST SP 800-88 (media sanitization scope) is not directly applicable, so
this is a judgement.

### 2. When a platform or GitHub step cannot complete

- **A. Block until it can** (retry with backoff, `state` stays at the step, an alert is
  raised): nothing is left behind silently; cost: a platform that is gone or an installation
  that is revoked blocks the deletion indefinitely.
- **B. Proceed with an explicit operator acknowledgement**: the admin calls
  `POST /admin/v1/tenant-deletions/{id}/skip-step` for the blocked step, which records what was
  left (`skipped`, `acknowledged_by`), is audited, and lets the database step run. Orphans
  remain on the platform, named in the record.
- **C. Proceed automatically after a timeout** (`TENANT_DELETION_STEP_TIMEOUT`): never blocks;
  silently leaves orphans, which an auditor may object to.

Lean: **B**, with A as the behaviour until the acknowledgement. Deleting the customer's data
from our side must not wait forever on a third party, and the left-behind state must be named
and attributed. Sources: EB-15; 0028 invariants 22-23 (orphan sweep reasoning).

### 3. The deleted tenant's other audit rows

- **A. Keep them until normal retention** (`AUDIT_RETENTION_DAYS`). Preserves the audit trail
  an auditor needs (what happened to this tenant before it was deleted); `before`/`after` may
  still hold the organisation login and repository full names for up to the retention period.
- **B. Keep them but reduce them** to ids and actions (clear `before` and `after` and strip
  names) through an owner-only definer function that the append-only trigger allows for this
  case alone. Removes tenant content from the trail while keeping its shape; costs a second
  owner-only mutation path against append-only (0032) and a rule for what is stripped.
- **C. Purge them** at deletion except the deletion events: a full erasure; costs the audit
  history (a SOC 2 reviewer cannot see what happened before) and needs a third owner-only
  path.

Lean: **A**. Audit data is the account of actions by admins (keyed by `sub`); it carries few
tenant data by design (redacted allow-lists, 0032), and it keeps the append-only guarantee
whole; revisit with B if a customer's erasure request demands it. Sources: EB-4, EB-15;
GDPR Art. 17(3)(b) and (e) (the right to erasure does not apply where retention is needed for
a legal obligation or legal claims), cited as general context and not as legal advice.

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
7. After `done`, the bodies of the webhook deliveries recorded for the tenant are null, and a
   delivery with no recorded tenant is left to normal retention and listed under `skipped`.
8. The platform step calls only adapter delete or revoke operations through the write-ahead
   path; with `delete_platform_resources` unset it deletes no project or repository (the
   lean for decision 1; adjust to the user's choice), and with it set it deletes exactly what
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
14. The audit purge removes the deletion's events only after retention, and the append-only
    triggers still reject updates and deletes of those rows for the app role.

**Failure policy**

15. A blocked step holds the deletion in that state and never advances to `database` without
    an acknowledgement (under the lean for decision 2); the acknowledgement is audited with
    the acknowledger's `sub` and what was left.
16. A tenant whose installation still exists is re-created with a new tenant id on its next
    event, and the deletion record and response say so.
17. The migration (the `tenant_deletions` table, the `webhook_deliveries.tenant_id` column)
    passes the `migration-compat` job and the previous release's suite (NFR-0027).

## Open issues

- **Re-creation.** Nothing stops a still-installed App from re-creating the tenant. A
  tombstone in `tenant_deletions` keyed on `(host, installation_id)` that makes
  `tb_upsert_tenant` refuse would prevent it, at the cost of blocking a legitimate reinstall;
  not part of this ADR.
- **Export before delete.** EB-15 lists "data export and deletion". A per-tenant data export is
  not designed here; the audit export (0032) and the read endpoints (0033) cover most of what a
  customer could ask for, and a dedicated export is a follow-up.
- **Backups.** Deleted rows persist in database backups until those expire; the backup
  retention is the deployment's (EB-6) and is documented in the Helm chart's notes (NG7).
- **Large tenants.** Batch size and a per-step time budget are settings; a tenant with very
  many repositories makes the GitHub step long, which the resumable cursor handles.
- **Deletion records' retention.** They hold ids and counts only and are purged with the audit
  retention; an alternative is to keep them indefinitely as a compliance record.

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
- The audit trail keeps tenant references past deletion (decision 3's lean), which is the
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
