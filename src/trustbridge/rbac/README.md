# rbac/

Role-based access for the admin API ([ADR 0033](../../../docs/adrs/0033-admin-api-v1-rbac-and-limits.md)).
No endpoints and no mounting here (NG6d). Imports `app.oidc` (authentication) and nothing from
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

## Wiring (NG6d)

`app.state.rbac = RbacRuntime(RbacConfig(client_id, separator, alias_personas), access)`, where
`access` implements `TenantAccess`: `get_tenant(tenant_id)` reads the tenant row (from
`tb_list_tenants()`) in its own session
and transaction and closes it before returning (ADR 0031: the `after_begin` hook only fires when a
transaction begins, so the check must not leave one open), and `tenant_context(tenant_id)` is
`trustbridge.tenancy.tenant_context`. The handler's session opens inside the context.

Integration invariant (verified in NG6d against the real module, not here): `require_role` enters
the sync `tenant_context` inside an async-generator dependency and holds it across the `yield`.
If `tenant_context` is ContextVar-backed, its set and reset must happen in one context and the
handler's session must see it; the unit tests only use a fake.

## Settings

`RBAC_ALIAS_TEMPLATE_PERSONAS` (default true) and `RBAC_ROLE_SEPARATOR` (default `:`, one
printable ASCII character that is not a letter, digit, `.` or `-`). The separator is a setting
because Keycloak's documentation does not confirm that `:` is accepted in client role names
(ADR 0033 decision 4); verify against a live realm before relying on the default.

## Not here

The `authz.denied` audit event (ADR 0033 invariants 3 and 5), the router-walk test (invariant 1)
and the `/me` endpoint belong to NG6d, which owns the router.
