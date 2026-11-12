# admin/

The admin API v1, read side ([ADR 0033](../../../docs/adrs/0033-admin-api-v1-rbac-and-limits.md),
NG6d-1 and NG6d-2). Mounted by `trustbridge.main` under `/admin/v1`; authentication and roles come from
`trustbridge.rbac`, the tenant context from `trustbridge.tenancy`, audit data from
`trustbridge.audit`. Imports `app.oidc` and `app.problem_details`; nothing in `src/app/` is edited.

- `routes.py`: the router and the handlers (read only; every route is a `GET`).
- `guards.py`: `guard(role, bucket, tenant_param=..., instance_only=...)` returns a route's `dependencies`: first
  `get_principal` (authentication), then the principal's rate-limit bucket (keyed on a hash of
  `(iss, sub)`; it runs before the role check so an over-limit caller costs no tenant lookup),
  then the RBAC dependency. Role guards are recorded in `GUARDED`, limit dependencies in
  `LIMITS`; the router-walk test fails for a route whose dependencies are not those three in that
  order (ADR 0033 invariant 1). `guard_any_grant` is for `/tenants`, which is filtered per grant.
  `instance_only=True` (with `tenant_param`) is for tenant disable and enable: the role is needed at
  instance scope (a tenant-scoped `admin` gets 403), a tenant the caller has no grant on is the same
  404 as an unknown one, and the tenant context is entered after the checks (it is
  `rbac.deps.require_role(..., instance_only=True)`; the order stays authenticate, limit, role and
  tenant check, so a denied 404 or 403 counts against the principal's bucket).
  `BoundedRoute` (the router's `route_class`) bounds the request body of every method but
  `GET`, `HEAD` and `OPTIONS` (the walk's `SAFE_METHODS`): a `Content-Length` over
  `ADMIN_MAX_BODY_BYTES` (default 16384) is a 413 before any byte is read, a malformed one a 400,
  and a chunked body is cut off with a 413 as it grows past the limit. The check runs before
  authentication (FastAPI reads the body before it resolves dependencies), so an unauthenticated
  caller can get a 413 or 400 that is not rate-limited or audited; that is the memory bound, accepted
  on purpose. A mutating handler is marked
  with `emits(action)` and uses `guard(..., "mutation")`; `mutations_without_emitter` is the router
  walk that fails for one that is not (ADR 0032 invariant 8). It finds the limit dependency by
  identity (the route needs exactly one, on the `mutation` bucket), requires a guard dependency
  from `GUARDED`, and reports any route that is not an `APIRoute` (a mount, a plain route) and
  whose path is `/admin/v1`, under it, or a prefix of it, since the guards cannot be seen inside it. No mutating route exists yet.
- `runtime.py`: `AdminRuntime` (`app.state.admin`: limiters, the host view, the session factory,
  the audit writer, the export lag and page size) and `build_limits`.
- `access.py`: `SqlTenantAccess`, the `TenantAccess` over `tb_get_tenant()`; `NoTenants` for
  `MODE=mock` without a database; `tenants_page`, the grant-filtered, keyset-paged tenant list
  (the filter and the limit run in the database).
- `queries.py`: the keyset reads behind repositories, resources, credentials, operations and
  deliveries (constant statements, bound parameters).
- `adapters.py`: `AdapterCatalog` (the enabled adapter classes plus the secret store listing) and
  `describe`, the capabilities an adapter class declares.
- `spec.py`: `python -m trustbridge.admin.spec` prints the OpenAPI snapshot.
- `problems.py`: problem `type` URIs and the limit on rejected requests (per address for a 401, per principal for an authenticated 403 or 404).
- `cursor.py`: the opaque cursor and `paginate`.
- `schemas.py`: one pydantic model per resource.
- `metrics.py`: the admin counters.

## Routes (v1, read side)

`{t}` is `{tenant_id}`. The tenant context is set only after the role check passes.

| Route | Min role / scope |
| --- | --- |
| `GET /me` | any authenticated caller |
| `GET /openapi.json` | any authenticated caller (generated from this router only) |
| `GET /hosts`, `/hosts/{host}` | `viewer`, instance (the answering replica's view, ADR 0030) |
| `GET /tenants` | any grant; lists the tenants the caller holds `viewer` on |
| `GET /tenants/{t}` | `viewer` on the tenant |
| `GET /tenants/{t}/repositories`, `/resources`, `/credentials`, `/operations` | `viewer` on the tenant |
| `GET /tenants/{t}/adapters` | `viewer` on the tenant |
| `GET /deliveries`, `/deliveries/{host}/{delivery_id}` | `viewer`, instance (the payload only for `operator`) |
| `GET /audit-events`, `/audit-events/export`, `/audit-events/{id}` | `auditor`, instance |
| `GET /tenants/{t}/audit-events`, `/tenants/{t}/audit-events/export` | `auditor` on the tenant |

**Tenant data** (one pydantic model per table, columns as snake_case, UUIDs as strings, RFC 3339
timestamps; JSONB columns come back as objects). Orders and cursors: repositories by numeric id;
resources and credentials by `(adapter, key)`; operations and deliveries newest first
(`(created_at, id)` and `(received_at, host, delivery_id)`). `credentials` carry state, names of
delivered outputs and timestamps, never a value: `views.py` allow-lists the keys of each JSONB
column (unknown keys are dropped) and passes `error`, `manual_action` and a delivery's `last_error`
through the audit log's `scrub_text`. A delivery has no `headers` anywhere; `GET
/deliveries/{host}/{delivery_id}` adds `payload` (the stored body parsed as JSON) for a caller
holding `operator` at instance scope, and `null` for a viewer, a purged body or a body that is not
JSON. `has_body` says whether a body is stored.

**Adapters.** `GET /tenants/{t}/adapters` lists the adapters named by `ADAPTERS_ENABLED` (entry-point
names; unset means none, an unknown name stops the process). Each item is `name`, `configured`
(whether the secret store lists `store:tenants/<uuid>/<adapter>`; only the listing is read, never a
value, ADR 0033 invariant 8) and `capabilities` (what the adapter class declares; `null` only if it
cannot say). An unavailable store is a 503. The api's `ADAPTERS_ENABLED` should match the
worker's: an api without it answers with an empty list, not "unknown".

**Tenants.** `/tenants` filters by grant and pages in the database. `/me` lists the first 200 tenants
the grants reach (by id) and sets `tenants_truncated` to true when more exist; use `/tenants` to
page through all of them. `TenantOut` has `admin_disabled_by`
and `admin_disable_reason`, set with `admin_disabled_at` by the disable action and `null`
otherwise; `tb_list_tenants()` and `tb_get_tenant()` return the stored values.

**The OpenAPI snapshot** `docs/openapi/admin-v1.json` is what `/openapi.json` serves. After any
route or schema change regenerate it with `python -m trustbridge.admin.spec >
docs/openapi/admin-v1.json`; the drift test (`tests/trustbridge/admin/test_spec_unit.py`) fails
otherwise, and the diff shows whether an existing route's schema changed (v1 is additive).

Lists take `limit` (1 to 200, default 50) and an opaque `cursor`, and answer
`{"items": [...], "next_cursor": "..." | null}`. A larger or non-positive `limit` is a 422.

**Audit parameters** are `AuditQuery`'s names: `since` (inclusive) and `until` (exclusive) on
`occurred_at` (RFC 3339 with a zone), `actor_sub`, `action`, `resource_type`, `resource_id`,
`outcome` (`success`, `denied`, `failure`), `descending` (list only) and, on the instance routes,
`tenant_id` to narrow. The export takes `since_seq` (resume after that `seq`, exclusive; ADR 0032's
name, mapped to `AuditQuery.after_seq`) instead of `cursor` and `limit`.

**The audit cursor** is an unsigned base64url JSON pair `[sort key, id]` (for audit, `[seq,
event id]`; for hosts and tenants, the name or id). It only names a position, and every query is
scoped by the caller's grants anyway, so a forged one can pick where to resume and nothing more. A
malformed one is a 400 `invalid-cursor`. Resuming after a key neither repeats nor skips rows that
were added or removed in between.

**The export** streams `application/x-ndjson` (`Content-Disposition: attachment`), one
`AuditRecord` JSON object per line in `seq` order, one short transaction per page of
`AdminRuntime.export_page_size` rows, only events older than `AUDIT_EXPORT_LAG_SECONDS`. After
the stream (or when it dies) the route writes the `audit.export` event (`audit/README.md`). An
error after the first byte cannot change the status; the event then says `complete: false`.

## Credential rotation (NG6e-5)

`POST /tenants/{t}/credentials/{adapter}/{scope_key}/rotate` (`operator`, on the tenant or at
instance scope) rotates one credential scope **synchronously in the api process**. `adapter` is at
most 64 characters (`[a-z0-9_-]`) and `scope_key` the 32-character hex key the credentials list
shows (at most 64, 422 otherwise). The api builds a `CredentialCore` (`admin/credentials.py`,
`build_runtime`) from the same parts the worker would use (ADR 0029): the enabled adapter classes
over the tenants' connection data, the SQL credential store whose saves append the credential
events (`credential.rotate.begin` and `.finish`, actor `api`), the lease manager, the parked
values, the GitHub output writer and environment inspector, and `SqlTenantGate`
(`tenancy/gate.py`, shared with the worker) as the core's gate. It needs a database; in
`MODE=mock` the route is a 503.

The answer is `{operation_id, status, change, wait_until, manual_action}`: ids, statuses and an
audit-safe message, never a credential value. Calling it again while a rotation is open resumes
that operation (a two-phase adapter keeps the old secret for an overlap and finishes on a later
call or by the reconciler). Errors, each with a fixed text and one `failure` event: 404 for an
adapter that is not enabled or a scope with no credential; 409 for a disabled tenant, a scope with
nothing rotatable (an OIDC scope), a lease held by another unit of work, or an adapter that is not
configured for the tenant; **502 `bad-gateway`** when the platform, GitHub or the secret store
fails (the exception, which may carry anything, is neither chained nor logged beyond its type
name). The success is one `credential.rotate` event (resource `credential`, id the scope key,
`after` `{adapter, operation_kind, operation_id}`), written after the core returns; the core's own
state changes are audited in their transactions. If that event cannot be written the call fails
(500) and the rotation stays resumable.

## Host registration (NG6e-4)

| Route | Min role / scope | Body | Answer |
| --- | --- | --- | --- |
| `POST /hosts/{host}/registration` | `operator`, instance | none | 200 `{post_url, manifest_json, state}` |
| `POST /hosts/{host}/registration/complete` | `operator`, instance | `{code, state}` | 200 ids and a reference |
| `POST /hosts/{host}/registration/manual` | `admin`, instance | `{app_id, pem, webhook_secret, slug?}` | 200 ids and a reference |

**The manifest flow.** `start` returns what the BFF renders: the URL to POST the manifest to
(`web_url` of the host), the manifest (its `hook_attributes.url` is `<PUBLIC_BASE_URL>/webhooks/github/<host>`
and its `redirect_url` `<PUBLIC_BASE_URL>/admin/github/registration/callback`) and a signed one-time
`state` bound to the caller (`iss` and `sub`). The BFF owns that callback path: GitHub redirects the
browser there with `code` and `state`, and the BFF forwards both, with the user's bearer token, to
`complete`. There is no public trustbridge callback. `complete` verifies the state (signature,
expiry, one use, host, caller), exchanges the code server-side, imports the PEM straight into the
host's signer and writes the webhook secret to the secret store; the answer has ids and the secret's
reference only. `manual` does the same storage for a hand-registered App (GHES enterprise Apps);
the PEM must be an unencrypted RSA key (the signer refuses anything else with a 422).

**Rules.** Only `kind=trustbridge` Apps are offered (an Argo CD App would write connection data,
which the admin API never does). A host that already has an App (configured with one, registered
in the database, or not `unregistered` in this replica) is a 409, so a key is never overwritten.
A registration (`complete`, `manual`) first takes the host's **claim**, a transaction-scoped Postgres
advisory lock held in an open transaction until the registration is saved, and only then checks
that the host is unregistered and writes anything. A second registration of the same host, on any
replica, finds the claim taken and is a 409 before it touches the key or the secret, so the loser
can never overwrite the winner's. The claim stores nothing (no row another process could read as a
registration, no migration) and a crashed process releases it with its connection. The registration
row is also inserted `ON CONFLICT DO NOTHING`. What can still be left over: if the secret store or
the database fails after the key was imported or the secret written and before the row is saved, the
host stays unregistered with a key (and possibly a secret) in place; the next registration attempt
replaces them. The claim holds one pooled connection for the length of the call (one GitHub
request for `complete`). A signer that cannot import a key is a 409 ("import it out of
band"). An invalid, expired, reused or foreign state, or a code GitHub refuses, is a 400 with a
fixed text. Without `PUBLIC_BASE_URL` and `REGISTRATION_STATE_KEY` (at least 32 characters, shared
by every replica) the three routes are a 503. The nonce is kept in Valkey (`SET EX`, `GETDEL`), in
the process without Valkey (`MODE=mock`); a deployment with more than one replica needs Valkey so
a `state` is one-use across them (the in-process store is per replica and never evicts).

**Audit.** Exactly one event per call that reaches the operation, on `resource_type=host`,
`resource_id=<host>`, no tenant: `host.registration.start` (after `{kind}`, written once the state is
issued), `host.registration.complete` and `host.registration.manual` (after `{kind, app_id,
app_slug, webhook_secret_ref}`), written in the transaction that saves the registration. Any other
outcome (a 409, a bad state, a signer or secret-store error) leaves a `failure` event in its own
transaction, best effort. A body that does not validate is a 422 before the operation and writes no
event, like every other validation error.

**Secrets.** The PEM, the webhook secret and the code are never returned, recorded in the audit
log or logged by trustbridge. These routes parse their body themselves (`registration.parse_body`)
because FastAPI's 422 detail echoes the offending input, which here would be the key or the secret;
a bad body is reported by field name only. The GitHub client logs each request at INFO, URL included, and the conversion URL contains the
one-time code (whoever holds an unspent one can exchange it for the key and the secret), so
`trustbridge.github.client` attaches a `logging.Filter` (`MaskManifestCode`) to the `httpx2` and
`httpx` loggers that rewrites the segment after `/app-manifests/` to `[MASKED]`. Nothing else in the
logging configuration changes.

## Delivery requeue and tenant reconcile (NG6e-3)

| Route | Min role / scope | Answer |
| --- | --- | --- |
| `POST /deliveries/{host}/{delivery_id}/requeue` | `operator`, instance scope | 200, the delivery as `GET` shows it |
| `POST /tenants/{t}/reconcile` | `operator`, instance or on the tenant | 202 `{"requested": true}` |

**Requeue** resets a `dead` delivery whose body still exists to `pending` (no attempts, no error,
no `dead_at`) in the request's transaction, together with its `delivery.requeue` event (resource
`host/delivery_id`, no tenant; `before` and `after` are the `delivery` shape, `last_error`
scrubbed). The relay or the periodic sweep then picks it up. A `pending` delivery is a 200 no-op
(its event has `before` equal to `after`); a `processed` one, or a dead one whose body was purged,
is a 409; an unknown one a 404. `host` is at most 63 and `delivery_id` at most 128 characters (422
above), so `host/delivery_id` fits an audit `resource_id`. The answer has the stored payload for
an operator and never the headers.

**Reconcile** records the tenant in the reconcile set (`ReconcileRequests`, Valkey `tb:reconcile`),
so a repeat is one request, and answers 202. The event (`tenant.reconcile`, no `before`/`after`)
is appended first, then the request recorded, then the transaction commits, so a queue failure
leaves no `success` event. A disabled tenant is a 409. **Nothing consumes the set yet**: the
reconcile loop is NG6i, so today the request is only recorded. Without a queue (no
`AdminRuntime.scheduler`) the route is a 503.

## Tenant disable and enable (NG6e-2)

| Route | Min role / scope | Body |
| --- | --- | --- |
| `POST /tenants/{t}/disable` | `admin`, instance scope (`instance_only`) | `{"reason": "..."}` |
| `POST /tenants/{t}/enable` | `admin`, instance scope (`instance_only`) | none |

Both answer 200 with the `TenantOut`. They call `tb_set_tenant_disabled` and append one
`tenant.disable` or `tenant.enable` event in the same transaction (`before` and `after` are the
`tenant_admin` shape). Both are idempotent and a call that changes nothing still writes its
`success` event, with `before` equal to `after`; a repeat disable keeps the first time, actor and
reason. `reason` is required, 1 to 500 printable characters, and is stored with secret-shaped
parts masked (`scrub_text`), so it never comes back verbatim. A failure rolls back and leaves a
`failure` event in its own transaction. A tenant-scoped `admin` gets 403 and a tenant the caller
has no grant on 404. Enabling a tenant with a non-terminal deletion (409) is NG6h's: the hook is
`v_deleting` in `tb_set_tenant_disabled`.

What a disabled tenant means (ADR 0033 invariant 19), enforced in the worker and the adapter
core, not here:

- **Webhooks** are stored and acknowledged but not handled. The worker looks the tenant up by the
  payload's installation id (`SqlTenantGate`, in `tenancy/gate.py`) and, for a disabled one, defers the delivery by
  `DISABLED_TENANT_RETRY_SECONDS` (default 15) without counting a failure. The next pass after
  `enable` handles it, so `enable` takes effect within that delay.
- **Adapter operations**: `CredentialCore` asks `CoreDeps.gate` (`OperationGate.allows`) before
  it starts an operation, refreshes a scope or sweeps orphans, and raises `TenantDisabledError`
  for a disabled tenant. An operation already in flight is resumed and finishes. The gate is
  given the operation kind so NG6h can let a deletion's own delete and revoke operations
  through. The api builds a `CredentialCore` for the rotate route with `SqlTenantGate`
  (`tenancy/gate.py`) as its gate (NG6e-5); the worker has none yet.
- The installation sync (`_sync`, `SqlTenants.upsert_installation`) never writes the columns.

**N-1 gap.** A pod of the release before this one does not know the columns: while a rolling
deploy mixes versions, an old worker still handles a disabled tenant's webhooks. Disable and
enable are complete once every worker runs this release.

## Problem types

Every error under `/admin/v1` is `application/problem+json` (ADR 0004) with `type`
`tag:trustbridge,2026:problem/<class>`, one stable class per kind of error:

| Class | Status | When |
| --- | --- | --- |
| `bad-request` | 400 | a request the server cannot parse |
| `invalid-cursor` | 400 | a malformed `cursor` |
| `unauthenticated` | 401 | no token or an invalid one (with the RFC 9728 challenge) |
| `forbidden` | 403 | a valid token whose roles are not enough |
| `not-found` | 404 | unknown route, host, tenant, delivery or audit event; also a tenant the caller has no grant on |
| `conflict` | 409 | requeue of a delivery that cannot be (processed, body purged), reconcile of a disabled tenant, a host that already has an App, a signer that cannot import a key |
| `payload-too-large` | 413 | a request body over `ADMIN_MAX_BODY_BYTES` |
| `validation` | 422 | a query parameter the schema rejects |
| `rate-limited` | 429 | over a bucket; `Retry-After` is the whole seconds left in the window |
| `internal` | 500 | an unhandled error (the detail is redacted outside `MODE=dev`) |
| `bad-gateway` | 502 | GitHub could not be reached or failed (5xx) while exchanging the manifest code; the one-time code is spent, so the registration is started again |
| `unavailable` | 503 | no database configured (`MODE=mock`), or the connection store cannot be listed |

The classes are open: v1 may add one. Errors off `/admin/v1` keep the template's `about:blank`.

## Rate limits (decision 5)

Fixed windows in Valkey through `trustbridge.ratelimit` (key prefix `tb:rl:admin:`), fail open with
a metric. Per principal (`iss` and `sub`, hashed): reads 600/min, exports 6/min, mutations 60/min
(`guard(..., "mutation")`). Per source address: 30/min for unauthenticated requests (a 401, or a route that does not exist;
over the limit the rejection becomes a 429). A 403 or 404 for an authenticated principal counts
against that principal's read bucket instead, so callers behind one address (a NAT, the BFF) do
not use up each other's budget. Settings `ADMIN_RATE_LIMIT_*`: `ENABLED`, `READ_REQUESTS`,
`MUTATION_REQUESTS`, `EXPORT_REQUESTS`, `ANONYMOUS_REQUESTS`, `WINDOW_SECONDS`, `TIMEOUT_MS`,
`EXEMPT_CIDRS`. Behind the BFF the address is the BFF's unless `TRUSTED_PROXIES` lists it.

## Not here yet

- `GET /tenant-deletions`, `/tenant-deletions/{id}`: there is no `tenant_deletions` table
  (ADR 0034); NG6h.
- The tenant-deletion `POST`s (NG6h). The consumer of the reconcile set is NG6i.
- The `transaction_timeout` / `AUDIT_EXPORT_LAG_SECONDS` startup check: NG6h.
- ADR 0033 stays Proposed until the mutating routes (NG6e) and the deletion reads (NG6h) exist.

## Don't

- Add a route without `guard(...)` as its first dependency, or outside `/admin/v1`.
- Read `store:tenants/<uuid>/<adapter>` values (connection data is operator-only).
- Put a secret, a connection value or a token in a response model.
