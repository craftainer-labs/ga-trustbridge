# admin/

The admin API v1, read side ([ADR 0033](../../../docs/adrs/0033-admin-api-v1-rbac-and-limits.md),
NG6d-1). Mounted by `trustbridge.main` under `/admin/v1`; authentication and roles come from
`trustbridge.rbac`, the tenant context from `trustbridge.tenancy`, audit data from
`trustbridge.audit`. Imports `app.oidc` and `app.problem_details`; nothing in `src/app/` is edited.

- `routes.py`: the router and the handlers (read only; every route is a `GET`).
- `guards.py`: `guard(role, bucket, tenant_param=...)` returns a route's `dependencies`: first the
  RBAC dependency (it authenticates), then the principal's rate-limit bucket. Every guard is
  recorded in `GUARDED`; the router-walk test fails for a route whose first dependency is not one
  (ADR 0033 invariant 1). `guard_any_grant` is for `/tenants`, which is filtered per grant.
- `runtime.py`: `AdminRuntime` (`app.state.admin`: limiters, the host view, the session factory,
  the audit writer, the export lag and page size) and `build_limits`.
- `access.py`: `SqlTenantAccess`, the `TenantAccess` over `tb_list_tenants()`; `NoTenants` for
  `MODE=mock` without a database.
- `problems.py`: problem `type` URIs and the per-address limit on rejected requests.
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
| `GET /audit-events`, `/audit-events/export` | `auditor`, instance |
| `GET /tenants/{t}/audit-events`, `/tenants/{t}/audit-events/export` | `auditor` on the tenant |

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
| `not-found` | 404 | unknown route, host or tenant; also a tenant the caller has no grant on |
| `conflict` | 409 | reserved (NG6e) |
| `validation` | 422 | a query parameter the schema rejects |
| `rate-limited` | 429 | over a bucket; `Retry-After` is the whole seconds left in the window |
| `internal` | 500 | an unhandled error (the detail is redacted outside `MODE=dev`) |
| `unavailable` | 503 | no database configured (`MODE=mock`) |

The classes are open: v1 may add one. Errors off `/admin/v1` keep the template's `about:blank`.

## Rate limits (decision 5)

Fixed windows in Valkey through `trustbridge.ratelimit` (key prefix `tb:rl:admin:`), fail open with
a metric. Per principal (`sub`, hashed): reads 600/min, exports 6/min (mutations 60/min, for
NG6e). Per source address: 30/min for rejected requests (401, 403, 404 count; over the limit the
rejection becomes a 429). Settings `ADMIN_RATE_LIMIT_*`: `ENABLED`, `READ_REQUESTS`,
`MUTATION_REQUESTS`, `EXPORT_REQUESTS`, `ANONYMOUS_REQUESTS`, `WINDOW_SECONDS`, `TIMEOUT_MS`,
`EXEMPT_CIDRS`. Behind the BFF the address is the BFF's unless `TRUSTED_PROXIES` lists it.

## Not here yet

- `GET /tenants/{t}/repositories`, `/resources`, `/credentials`, `/operations`, `/deliveries` and
  the snapshot `docs/openapi/admin-v1.json` with its drift test: NG6d-2.
- `GET /tenants/{t}/adapters`: nothing on main loads adapters in the api process (no
  `ADAPTERS_ENABLED`, no registry), so there is no capability summary to serve; it needs that
  wiring first (NG6d-2).
- `GET /audit-events/{id}`: neither `tb_audit_list` nor a plain `SELECT` can read one null-tenant
  event by id without a new database function (a migration); NG6d-2.
- `GET /tenant-deletions`, `/tenant-deletions/{id}`: there is no `tenant_deletions` table
  (ADR 0034); NG6h.
- Every `POST` (NG6e). `admin_disabled_by` and `admin_disable_reason` are not in `TenantOut`
  because `tb_list_tenants()` does not return them yet.
- The `transaction_timeout` / `AUDIT_EXPORT_LAG_SECONDS` startup check: NG6h.

## Don't

- Add a route without `guard(...)` as its first dependency, or outside `/admin/v1`.
- Read `store:tenants/<uuid>/<adapter>` values (connection data is operator-only).
- Put a secret, a connection value or a token in a response model.
