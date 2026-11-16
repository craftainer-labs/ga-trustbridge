# NFR-0030. Authorization is server-side RBAC from IdP client roles, with per-tenant scoping

## Status

Accepted. Implemented.

## Attribute

Security (authentication, authorization, least privilege), portability.

## Description

- **Roles come from the token, in one place.** The admin API reads roles only from the Keycloak
  client roles `resource_access.<OIDC_CLIENT_ID>.roles`, in one function that builds the
  `Principal` (`sub`, `iss` and the set of `(role, scope)` grants). Users are keyed by the
  durable `sub` and `iss`, never by email or login. A token without `resource_access` yields a
  principal with no roles, not an error. A role name that is neither a template persona nor a
  recognised trustbridge name grants nothing.
- **Four roles on a ladder** (`viewer`, `operator`, `admin`, plus `auditor` outside the ladder, so
  an `admin` or `operator` cannot read audit). Instance scope is `trustbridge:<role>`; tenant
  scope is `trustbridge:<host>:<org>:<role>`. The template personas are aliased at instance scope
  only while `RBAC_ALIAS_TEMPLATE_PERSONAS` is on, and grant nothing otherwise.
- **Tenant scoping.** An instance-scoped endpoint rejects a tenant-scoped grant; a grant on tenant
  A confers nothing on tenant B. A caller without a grant on a tenant gets the same 404 as for an
  unknown one, so existence is not revealed. A renamed organisation stops matching its old role
  names at once.
- **Order and denials.** Authentication, then the rate limit, then the role check, then the
  tenant context; the context is entered only after the role check passes. A 403 or a tenant 404
  writes an `authz.denied` audit row carrying the `sub`, subject to the denial throttle of
  [NFR-0031](NFR-0031-audit-log-is-append-only-complete-and-exportable.md): past a configurable
  burst (`AUDIT_DENIED_BURST`, default 5 per subject and reason per 60 s window) further denials
  are counted into a summary row, not written one by one.
- **Portability limit (NFR-0014).** The role source is Keycloak-shaped. Other IdPs reach it
  through a Keycloak broker or a protocol mapper that writes the same claim shape; only `sub` is
  assumed otherwise.

## Source

Enterprise baseline rows EB-1 (SSO through OIDC, SAML via an IdP broker, users keyed by durable
id) and EB-2 (RBAC with roles mapped from the IdP) of the plan
`docs/plans/2026-10-craftainer-labs-apps.md`; user decisions 2-4 recorded in
[ADR 0033](../adrs/0033-admin-api-v1-rbac-and-limits.md) (G-api, 2026-11-03: Keycloak client
roles rather than a groups claim). Stakeholder: Security / compliance. Builds on
[NFR-0011](NFR-0011-server-side-authorization-only.md),
[NFR-0012](NFR-0012-routes-public-by-default.md) and
[NFR-0014](NFR-0014-oidc-claim-shape-portability.md).

## Verification

Run in the Checks workflow's prek job (pytest, 100% coverage gate).

- Claim parsing and the role model (ADR 0033 invariants 4, 19a, 20):
  `tests/trustbridge/rbac/test_claims.py` (`test_parse_grants`,
  `test_personas_grant_nothing_when_the_alias_is_off`, `test_a_missing_or_unusable_sub_is_no_principal`),
  `tests/trustbridge/rbac/test_model.py` (`test_ladder`, `test_instance_roles_ignore_tenant_grants`,
  `test_renamed_org_stops_matching_the_old_role_name`, `test_grant_fails_closed_on_half_scope_or_bad_role`).
- The dependency (invariants 2, 3, 5-7): `tests/trustbridge/rbac/test_deps.py`
  (`test_no_token_is_401_problem_with_the_challenge`, `test_audit_is_not_on_the_ladder`,
  `test_instance_route_rejects_a_tenant_scoped_grant`,
  `test_tenant_grant_on_another_tenant_is_404_and_never_enters_the_context`,
  `test_tenant_route_runs_the_check_before_the_context`,
  `test_principal_with_no_roles_gets_404_on_a_tenant_route`).
- Denial audit rows: `tests/trustbridge/rbac/test_denial_audit.py`;
  `tests/trustbridge/admin/test_guards_unit.py`; integration
  `test_denials_are_rows_and_audit_reads_are_scoped_by_grant` in
  `tests/trustbridge/admin/test_admin_integration.py`.
- Guard order and the route walk (invariants 1, 13, 21):
  `test_every_admin_route_authenticates_limits_then_checks_the_role_under_v1` in
  `tests/trustbridge/admin/test_routes_unit.py`;
  `test_the_mutation_walk_requires_a_guard_dependency` in `test_guards_unit.py`.
- Tenant-scoped admin cannot reach instance actions through the stack:
  `test_a_tenant_scoped_admin_cannot_disable_through_the_stack` in
  `tests/trustbridge/admin/test_reads_integration.py`.

Known gap: no end-to-end test against a real Keycloak token for the namespaced client roles; the
claim shape is tested with synthetic tokens only.
