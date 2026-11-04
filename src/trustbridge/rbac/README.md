# rbac/

Role-based access for the admin API ([ADR 0033](../../../docs/adrs/0033-admin-api-v1-rbac-and-limits.md)).
No endpoints here: `trustbridge.admin` mounts the routes and `trustbridge.main` builds
`app.state.rbac`. Imports `app.oidc` (authentication) and nothing from
`trustbridge.tenancy`: the tenancy layer is reached through the `TenantAccess` Protocol.

- `model.py`: `Role` (admin, operator, auditor, viewer), the ladder (`confers`: viewer < operator
  < admin, auditor includes viewer, only auditor confers auditor), `Grant`, `Principal`,
  `TenantRef`, and the scope matching (`instance_roles`, `tenant_roles`).
- `claims.py`: `parse_grants` / `build_principal`, pure. Roles come only from
  `resource_access.<OIDC_CLIENT_ID>.roles`. `trustbridge<sep><role>` is instance scope,
  `trustbridge<sep><host><sep><org><sep><role>` is tenant scope (host and org case-insensitive, role
  exact); template personas (`viewer`, `security`, `detective`, `editor`, `maintainer`) alias to
  instance roles when `RBAC_ALIAS_TEMPLATE_PERSONAS` is on; everything else grants nothing.
- `deps.py`: `require_role(role, tenant_param=None)`. Instance routes need an instance grant
  (403 otherwise). Tenant routes (`tenant_param` names the path parameter) accept an instance grant
  or a grant on that tenant; an unknown, malformed or ungranted tenant is the same 404, a too-weak
  grant is 403, and only then is `TenantAccess.tenant_context` entered. The actor is `sub`.

## Wiring

`app.state.rbac = RbacRuntime(RbacConfig(client_id, separator, alias_personas), access)`, where
`access` implements `TenantAccess`: `get_tenant(tenant_id)` reads the tenant row (from
`tb_list_tenants()`) in its own session
and transaction and closes it before returning (ADR 0031: the `after_begin` hook only fires when a
transaction begins, so the check must not leave one open), and `tenant_context(tenant_id)` is
`trustbridge.tenancy.tenant_context`. The handler's session opens inside the context.

Integration invariant, verified against the real module and a Postgres with RLS enforced
(`tests/trustbridge/admin/test_admin_integration.py`): `require_role` enters the sync
`tenant_context` inside an async-generator dependency and holds it across the `yield`; the
handler's session sees only that tenant, and the context is reset after the response, also when the
handler raised. The unit tests here only use a fake. `admin/access.py` is the real `TenantAccess`
(`SqlTenantAccess`, reading `tb_list_tenants()`); `rbac` still imports nothing from `tenancy`
(import-linter contract).

## Settings

`RBAC_ALIAS_TEMPLATE_PERSONAS` (default true) and `RBAC_ROLE_SEPARATOR` (default `:`, one
printable ASCII character that is not a letter, digit, `.` or `-`). The separator is a setting
because Keycloak's documentation does not confirm that `:` is accepted in client role names
(ADR 0033 decision 4); verify against a live realm before relying on the default.

## Not here

The router-walk test (invariant 1) and `/me` live with the router, in `trustbridge.admin`.

## Denials and the audit log

`RbacRuntime.denials` (a `DenialSink`, optional; `Denial` and `DenialSink` live in
`trustbridge.audit.denial` and are re-exported here, so `rbac` depends on `audit`, never the
reverse) receives a `Denial` for every denial of an
authenticated principal, before the 403 or 404 is raised: a role that is missing (instance or
tenant route) and a tenant that is unknown, malformed or not granted. The sink is
`trustbridge.audit.AuditDenials` (ADR 0032: a row with a null `tenant_id`, its own transaction;
ADR 0033 invariants 3 and 5). It is awaited, so a sink failure fails the request rather than
answering as if the denial were recorded. A rejected, unauthenticated request never reaches it
(logs and metrics only). `Denial.tenant_id` is the requested id only when the path value is the
canonical UUID spelling; `source_ip` is the ASGI client address when it is a plain IP, and
`request_id` the caller's `X-Request-ID` when it is up to 128 of `[A-Za-z0-9._:-]`; anything else
is dropped, not stored. `trustbridge.main` sets the sink when it builds `app.state.rbac`: `AuditDenials` wrapped in
`audit.throttle.ThrottledDenials` (see `audit/README.md`). `client_ip` and `safe_request_id` are
the helpers that produce `source_ip` and `request_id`; `admin.guards` reuses them.
