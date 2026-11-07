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
and `admin_disable_reason`, set with `admin_disabled_at` by the disable action (NG6e-2) and `null`
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
| `conflict` | 409 | reserved (NG6e) |
| `payload-too-large` | 413 | a request body over `ADMIN_MAX_BODY_BYTES` |
| `validation` | 422 | a query parameter the schema rejects |
| `rate-limited` | 429 | over a bucket; `Retry-After` is the whole seconds left in the window |
| `internal` | 500 | an unhandled error (the detail is redacted outside `MODE=dev`) |
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
- Every `POST` (NG6e-2 to NG6e-5), except the tenant-deletion ones (NG6h). The columns, the
  instance-only tenant guard and the body bound they need exist (NG6e-1).
- The `transaction_timeout` / `AUDIT_EXPORT_LAG_SECONDS` startup check: NG6h.
- ADR 0033 stays Proposed until the mutating routes (NG6e) and the deletion reads (NG6h) exist.

## Don't

- Add a route without `guard(...)` as its first dependency, or outside `/admin/v1`.
- Read `store:tenants/<uuid>/<adapter>` values (connection data is operator-only).
- Put a secret, a connection value or a token in a response model.
