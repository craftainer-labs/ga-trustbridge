# 0033. Admin API v1: base path, roles, tenant scoping, rate limits and the OpenAPI contract

## Status

Proposed. G-api answers from the user are recorded below (2026-11-03); none are open.
NG6d-1 (#23) implemented `me`, openapi, hosts, tenants and audit read/export. NG6d-2 implemented
the rest of the read side except `/tenant-deletions`: repositories, adapters, resources,
credentials, operations, deliveries, `GET /audit-events/{id}` and the committed OpenAPI snapshot.
NG6e-1 added the foundation for the mutating routes (the tenant admin-disable columns and
`tb_set_tenant_disabled`, the instance-only tenant guard, the request body bound and the mutation
router walk). NG6e-2 added tenant disable and enable with their enforcement in the worker and the
adapter core. NG6e-3 added delivery requeue and tenant reconcile (the reconcile request is
recorded; its consumer is NG6i); NG6e-4 added the host registration routes (start, complete, manual); NG6e-5 added credential rotate, run in the api. Still open, so this stays Proposed:
the tenant-deletion routes, reads and
writes (`GET /tenant-deletions`, `GET /tenant-deletions/{id}`, `POST /tenants/{t}/deletion`,
`POST /tenant-deletions/{id}/cancel`, `POST /tenant-deletions/{id}/skip-step`; NG6h, with
[0034](0034-tenant-data-deletion.md)).
It becomes Accepted when the implementation (NG6b RBAC, NG6d read endpoints, NG6e mutating
endpoints) merges. Builds on
[0028](0028-trustbridge-architecture-and-instance-layout.md) (instance layout, the abuse
limiter, the NG6 split of limits),
[0030](0030-key-signer-and-github-hosts.md) (host state, registration),
[0031](0031-tenancy-and-row-level-security.md) (tenant context) and
[0032](0032-audit-log.md) (what is audited) and [0034](0034-tenant-data-deletion.md) (tenant data deletion). Inherits [0004](0004-uniform-problem-details-error-responses.md)
(Problem Details), [0003](0003-auth-strategy-and-federated-backends.md) and
[0026](0026-resource-server-metadata-and-bff.md) (OIDC resource server, BFF), NFR-0001,
NFR-0002, NFR-0011, NFR-0012 and NFR-0014 from the template.

## Context

EB-2 (RBAC with roles mapped from IdP groups), EB-3 (tenants), EB-4 (audit) and EB-11
(versioned, OpenAPI, rate limited with `Retry-After`) need an administrative API that the UI
(NG8, through its BFF) and operators call. What exists:

1. **Mounts in the template app.** `/health`, `/crud/v1` (and `/api/_crud`), `/audit` (an
   example role-protected route), `/mock/token` (MODE=mock only), and the RFC 9728 well-known
   routes (`app/main.py:212-242`). trustbridge's own routes today are
   `/webhooks/github/{host}` and `/webhooks/argocd/{tenant_id}`. The manifest flow's callback
   is a fixed constant, `CALLBACK_PATH = "/admin/github/registration/callback"`
   (`github/manifest.py:48`); the manifest flow's admin endpoints are written but unmounted
   (`trustbridge/README.md`, "Not wired yet").
2. **NFR-0001**: every resource under an explicit `/vN` prefix, no unversioned alias.
   NFR-0002: Sunset/Deprecation headers on a deprecated version. NFR-0011: server-side
   authorization only. NFR-0012: a route is public unless it declares an auth dependency.
3. **Roles in the template.** `require_roles(*roles)` reads
   `claims["resource_access"][OIDC_CLIENT_ID]["roles"]`, the Keycloak client-role shape;
   NFR-0014 says that shape is not portable and nothing but `sub` may be assumed. The
   template's example personas are `security` and `detective` (`app/controllers/audit.py`).
4. **Rate limits in the template.** `app/rate_limit.py` is a per-route fixed-window limiter
   (the `limits` library over Valkey, keyed on the client address plus the handler name),
   applied by decorator, with defaults `rate_limit_mock_token = "10/minute"` and
   `rate_limit_bulk_action = "20/minute"`. 0028 added a second, webhook-specific limiter
   (`trustbridge/webhooks/limiter.py`: fixed window, `INCR` and `EXPIRE NX` in one
   transaction, fails open under a 50 ms budget, IPv6 keyed by /64). 0028 states NG6 may reuse
   that component but owns the API-side policy.
5. **Connection data is operator-only.** The NG5f and NG5d notes in the adapter READMEs record
   that `connection.base_url` receives adapter admin tokens, so a writer of it is an SSRF
   vector.
6. **Installation sync.** The installation `_sync` writes tenant state `active` or
   `suspended` from the GitHub API on every installation event, and a NG34f note says an admin
   state must not be overwritten by it.
7. **Template pagination.** The generic CRUD uses `skip`/`limit` (ADR 0008).
8. **The UI's needs** (plan, "Track NG"): installations and repositories, platform
   connections, policies, delivery and job status, an audit viewer.

## Decision

### Already decided (cited, not reopened)

- **Adapter connection data stays operator-only.** The admin API never writes
  `store:tenants/<uuid>/<adapter>`. It exposes read-only metadata per tenant and adapter: the
  adapter name and whether it is configured (yes/no), plus its capability summary, never the
  stored values, URLs or tokens. Reason: `connection.base_url` receives adapter admin tokens
  (SSRF); any future writer needs its own SSRF review (NG5f, NG5d notes).
- **Tenant disable is a separate column** that the installation `_sync` never
  touches (see "Tenant disable"). The effective state is the combination of both.
- **Roles are tied to RLS by 0031**: the tenant context is set only after authorization.
- **Rate limits are the API's own**; the webhook limiter keeps its own settings (0028).

### Design that follows the template (judgement calls, listed so the user can overrule)

These are not open questions to the user because a template ADR or NFR or common practice
settles them; they are recorded for visibility.

- **Authentication.** The template's bearer validation (`get_current_claims`), issuer and
  audience checks, unchanged. No cookies, no sessions in the API (the BFF owns those, ADR 0026).
- **Deny by default (NFR-0011, NFR-0012).** Every admin route declares exactly one role
  requirement; a route without one fails a test that walks the router (invariant 1). The API
  never relies on the UI to hide an action.
- **Scope of a role grant.** A principal holds roles at **instance scope** (all tenants and the
  host-level endpoints) or at **tenant scope** (one tenant). Host-level data (hosts,
  deliveries, registration) requires an instance-scope role because it is not tenant-isolated
  (0031, Open issues).
- **Tenant context.** A route with `{tenant_id}` in its path runs the RBAC check first, then
  sets `tenant_context(tenant_id)` (0031) for the request. A tenant the principal has no
  grant on answers **404**, the same as an unknown tenant, so ids cannot be enumerated.
- **Errors.** Every error, including 401 (with the RFC 9728 `WWW-Authenticate`), 403, 404, 409,
  422 and 429, is `application/problem+json` through ADR 0004's handlers. 429 carries
  `Retry-After`. The problem `type` is a stable URI per class and `detail` is redacted outside
  `MODE=dev` (NFR-0015).
- **Compatibility.** v1 changes are additive (new endpoints, new optional fields, new enum
  values documented as open); a breaking change is a v2 mount, with NFR-0002's deprecation
  headers on the old one.
- **Cross-cutting.** Every mutating call is audited (0032) in the same transaction as the
  change; every list endpoint is bounded; request bodies are bounded.

### Roles and their source

The role set and the persona aliases are user decision 2 below. Roles are read from the Keycloak client roles in
`resource_access.<OIDC_CLIENT_ID>.roles`, the template's `require_roles` path, by the RBAC
dependency (`trustbridge.rbac`), which turns the token into a `Principal`: `sub`, `iss`, and the
set of `(role, scope)` grants described under "User decisions" items 3 and 4. A role name that
is neither a template persona (aliased, decision 2) nor a trustbridge role grants nothing.

### Tenant disable (user decision 8: in NG6)

A tenant row gets `admin_disabled_at timestamptz null`, `admin_disabled_by` (the actor's `sub`)
and `admin_disable_reason`, all separate from `state`. `tb_upsert_tenant` and the installation
`_sync` (0031) never write them. A tenant is **effective-active** only when `state = 'active'`
and `admin_disabled_at IS NULL`. A disabled tenant: its webhooks are stored and acknowledged
but their handlers are skipped (left unprocessed, like a delivery waiting for a host that is not
ready, 0028 item 3, and re-relayed when it is enabled again); no new adapter operation starts (except the adapter delete and revoke operations of a
deletion in progress, 0034);
in-flight operations finish or are abandoned by the normal rules; the admin API still reads it.
It is reversible (`enable`), audited, and needs the `admin` role at instance scope. A tenant
with a non-terminal deletion (0034) cannot be enabled.

## User decisions (G-api, 2026-11-03)

Recorded from the user's answers. Item 2 (roles) is recorded after the personas analysis, in the next section.

### 1. Base path: `/admin/v1`

**User decision (G-api, 2026-11-03):** the admin API is mounted under `/admin/v1/...`. The
reasons: an ingress can expose `/webhooks` publicly and `/admin` internally, and the manifest
callback `/admin/github/registration/callback` already lives under `/admin`. The template's
`/crud/vN`, `/audit` and `/mock` mounts are untouched. **Note for NG6g:** NFR-0001 says every
resource is mounted under an explicit `/vN` prefix; `/admin/v1` carries the version as the
second segment, so NG6g clarifies NFR-0001 to say "a `/vN` path segment immediately after the
mount prefix, and no unversioned alias". The browser redirect that ends the manifest flow
is the BFF's route, not trustbridge's: **user decision (NG6e, 2026-11-06):** the BFF receives the
redirect and forwards `code` and `state` with the user's bearer token to
`POST /admin/v1/hosts/{host}/registration/complete`, so trustbridge mounts no public callback
and every admin route is under `/admin/v1`.

### 3. IdP mapping: Keycloak client roles

**User decision (G-api, 2026-11-03):** roles come from the Keycloak client roles
`resource_access.<OIDC_CLIENT_ID>.roles`, the template's existing `require_roles` path, not a
configurable groups claim (the earlier lean was the groups claim; the user chose otherwise).
Consequence, recorded for NFR-0014: the role source is Keycloak-shaped and not portable to an
IdP that does not emit `resource_access`. An Entra or Okta deployment must go through a Keycloak
broker (EB-1 already expects an IdP broker) or a protocol mapper that writes the same claim
shape. The `Principal` builder keeps the claim path in one function so a second source can be
added later without touching routes. NFR-0014 stays accurate: nothing but `sub` is assumed
present, and a token without `resource_access` yields a principal with no roles.

### 4. Tenant scoping: per-tenant names, as Keycloak client roles

**User decision (G-api, 2026-11-03):** the per-tenant naming `trustbridge:<host>:<org>:<role>`.
Decision 3 makes the carrier a Keycloak **client role**, not a group, so the name applies to
the role. The two decisions fit together as follows; no genuine conflict arises.

- **Role names.** In the client named by `OIDC_CLIENT_ID` the roles are:
  - instance scope: `trustbridge:<role>`, for example `trustbridge:admin`;
  - tenant scope: `trustbridge:<host>:<org>:<role>`, for example
    `trustbridge:github.com:acme:operator`, where `<host>` is the `GITHUB_HOSTS` name (config
    validation rejects a host name containing `:`), `<org>` is the organisation login
    (GitHub logins contain no `:`) and `<role>` is one of the four role names.
  - The parser splits on `:` into exactly two or four parts after requiring the `trustbridge`
    prefix; anything else is ignored. Matching is case-insensitive for host and org (GitHub
    logins are case-insensitive) and exact for the role.
- **How the pair resolves to a tenant.** The principal's tenant grants are resolved against
  `tenants.host` and `tenants.org` through `tb_list_tenants()` (0031); a role naming an org with
  no tenant grants nothing. The org login can be renamed on GitHub; `_sync` updates
  `tenants.org` from the installation event, after which the old role name no longer matches and
  the Keycloak admin renames the role. The tenant row's `org_id` is the durable id; names carry
  the login (the accepted weakness of the user's choice, mitigated by the rename being visible
  and failing closed).
- **How a Keycloak admin assigns it.** Per tenant, the admin creates the client roles in the
  `OIDC_CLIENT_ID` client (a client role must exist before it is assigned; trustbridge does not
  create them). The convenient pattern is a Keycloak **group per tenant and role**, for example
  `tenants/github.com/acme/operators`, with the matching client role under that group's *Role
  mapping* tab (Keycloak groups accept client role mappings); users are added to the group.
  Keycloak's role mappers put a user's effective client roles, including those inherited from
  groups, into `resource_access.<client>.roles` (Keycloak server admin guide, "Role mappings" and
  "Groups"). Direct user role mappings and composite roles work equally. A deployment that
  wants one bundle can define a composite client role per tenant.
- **Token size.** With *Full scope allowed* (the default) every client role of the user is
  emitted. With many tenants this grows the token; Keycloak's remedy is to turn *Full scope
  allowed* off for the client and scope-map the roles; or the user belongs only to the groups
  they need. A user with access to hundreds of tenants should hold instance scope instead.
- **To verify at implementation (NG6b):** that Keycloak accepts `:` in client role names (the
  template's `realm-export.json` roles use plain names). If it does not, the separator becomes
  a configurable character (`RBAC_ROLE_SEPARATOR`, default `:`) and the format is otherwise
  unchanged. The ADR treats this as a verification, not a conflict.
- **Dev realm.** The devcontainer's realm export (`.devcontainer/stack/keycloak/`) defines the
  template's five roles only. Adding the trustbridge roles and users there is a
  `.devcontainer/` change that only the user can rebuild; tests use `MODE=mock` tokens, which
  carry the same claim shape, until then.

### 5. Rate limits

**User decision (G-api, 2026-11-03):** keyed on the authenticated principal (`sub`) after
authentication and on the source address before it. Defaults, all settings
(`ADMIN_RATE_LIMIT_*`): **600/minute per principal for reads, 60/minute per principal for
mutations, 30/minute per source address for unauthenticated requests (a 401), about
6/minute per principal for exports**. A 403 or 404 for an authenticated principal counts against
that principal's read bucket (keyed `(iss, sub)`), not the address's, so callers behind one
address do not use up each other's budget (user decision, NG6d-2). Fixed window; 429 Problem Details with `Retry-After`
(whole seconds left in the window, at least 1); **fail open** when Valkey errors or exceeds the
limiter's budget, with a metric. It reuses NG3's Valkey limiter with the key prefix
`tb:rl:admin:`; because that limiter lives in `trustbridge.webhooks`, reusing it moves its
counter and fail-open core up a layer, which NG6 does inside its write set (open issue below).
The numbers are starting points to measure with the UI.

### 6. OpenAPI v1

**User decision (G-api, 2026-11-03):**

- **Pagination: cursor.** `limit` (default 50, maximum 200) and an opaque `cursor`; the
  response is `{ "items": [...], "next_cursor": "..." | null }`. No total count by default.
- **Spec: served and snapshotted.** Served at `/admin/v1/openapi.json`, **authenticated** (any
  role), generated from the admin router only (FastAPI `get_openapi` with that router's
  routes, so the template's example routes and its CRUD stay out), plus a committed snapshot
  (`docs/openapi/admin-v1.json`) with a **drift test** that fails when the generated spec
  differs from the snapshot. The snapshot is attached to each release.

**Endpoint list.** Base `/admin/v1`; `{t}` is a tenant id. Roles are the four of decision 2.

Read-only (NG6d):

| Endpoint | Scope | Min role |
| --- | --- | --- |
| `GET /me` (the principal: `sub`, roles, tenants) | any | any |
| `GET /hosts`, `GET /hosts/{host}` (state, reason, answering replica; 0030) | instance | viewer |
| `GET /tenants`, `GET /tenants/{t}` (effective state, both state columns) | per grants | viewer |
| `GET /tenants/{t}/repositories` | tenant | viewer |
| `GET /tenants/{t}/adapters` (name, configured yes/no, capabilities; never connection data) | tenant | viewer |
| `GET /tenants/{t}/resources`, `/credentials` (state and timestamps, never a value), `/operations` | tenant | viewer |
| `GET /deliveries`, `GET /deliveries/{host}/{id}` (status, attempts, error; body only for `operator`) | instance | viewer |
| `GET /tenant-deletions`, `GET /tenant-deletions/{id}` (0034) | instance | viewer |
| `GET /tenants/{t}/audit-events`, `GET /tenants/{t}/audit-events/export` (NDJSON, 0032) | tenant | auditor |
| `GET /audit-events`, `GET /audit-events/{id}`, `GET /audit-events/export` (all tenants and null-tenant events) | instance | auditor |
| `GET /openapi.json` | any | any |

Mutating (NG6e, except the rows marked NG6h, which belong to the deletion item because they need
the `tenant_deletions` table of 0034):

| Endpoint | Scope | Min role |
| --- | --- | --- |
| `POST /deliveries/{host}/{id}/requeue` | instance | operator |
| `POST /tenants/{t}/reconcile` | tenant | operator |
| `POST /tenants/{t}/credentials/{adapter}/{scope_key}/rotate` (`scope_key` path-encoded and bounded) | tenant | operator |
| `POST /hosts/{host}/registration` (start the manifest flow) | instance | operator |
| `POST /hosts/{host}/registration/complete` (the BFF forwards the manifest flow's `code` and `state`; 0030) | instance | operator |
| `POST /hosts/{host}/registration/manual` (App id, PEM, webhook secret; 0030) | instance | admin |
| `POST /tenants/{t}/disable`, `POST /tenants/{t}/enable` | instance | admin |
| `POST /tenants/{t}/deletion` (0034; the tenant must be disabled), `POST /tenant-deletions/{id}/cancel` (before the first destructive step) **(NG6h)** | instance | admin |
| `POST /tenant-deletions/{id}/skip-step` (0034, user decision 2) **(NG6h)** | instance | admin |

Not in v1 by design: any write of adapter connection data (decided), a policies endpoint (no
policy table exists yet; the UI's "policies" screen needs a separate item and ADR), and any
role-mapping endpoint (roles live in Keycloak, decisions 3 and 4).

### 7. Audit

**User decision (G-api, 2026-11-03):** recorded in [0032](0032-audit-log.md): NDJSON export plus
the JSON list endpoint; append-only through grants, the trigger and purge only through the
definer function; unauthenticated rejections to logs and metrics, `authz.denied` as rows; push
to a SIEM is a follow-up.

### 8. Tenant disable

**User decision (G-api, 2026-11-03):** in NG6, as designed under "Tenant disable" above.

### 9. EB-15 tenant data deletion

**User decision (G-api, 2026-11-03):** in NG6 (the user overrode the lean to defer it). The
design is large enough for its own ADR: [0034](0034-tenant-data-deletion.md).

## Roles and the template's personas

The user decided to start from the four roles and asked that the template's existing personas
be examined. The analysis is here; the decision is item 2 at the end of this section.

### Where the template's personas are defined and used

The template ships five roles, all Keycloak client roles of the `api` client:
`viewer`, `editor`, `maintainer`, `security`, `detective`.

- **Defined** in the devcontainer's Keycloak realm
  (`.devcontainer/stack/keycloak/realm-export.json`, one test user per role), and as the list
  `ALL_ROLES` in `tests/unit/conftest.py`.
- **Used** in two places. The Hero example resource (`app/crud_1/heroes/heroes_v2.py:128-130`,
  FR-0015): read needs `viewer`, `editor`, `maintainer` or `detective`; create and update need
  `editor` or `maintainer`; delete needs `maintainer` alone. The `/audit` example route
  (`app/controllers/audit.py`, FR-0016): `security` or `detective`, returning the caller's
  subject and roles. E2E journeys exist per persona (`tests/e2e/<persona>/`).
- **Documented** as personas of the example, not a model for real applications:
  `app/README.md` (RBAC section) and NFR-0014 say only `sub` may be assumed and the
  `resource_access` shape is Keycloak-specific. `require_roles` is the mechanism; the names are
  example data.

### Analysis

| Template role | What it means in the template | Fit in trustbridge |
| --- | --- | --- |
| `viewer` | reads Heroes | Same idea as ours (read-only), but the name is shared, so a hero-demo `viewer` would silently gain access to tenant data if trustbridge used the bare name |
| `editor` | creates and updates Heroes | Closest to `operator` in power, but the meaning is "edit records"; trustbridge's operator performs operations (requeue, reconcile, rotate), not record edits |
| `maintainer` | all Hero writes, and delete of his own record (ADR 0011) | Closest to `admin`, but its delete is owner-scoped on example data; granting it admin on tenants would be a privilege jump |
| `security` | reads `/audit` | Matches `auditor` in intent (audit read, no change) |
| `detective` | reads `/audit` and Hero data, cannot write either | Matches `auditor` exactly (reads everything, mutates nothing) |

### 2. Roles: the four roles, with the template personas aliased

**User decision (G-api, 2026-11-03):** the role set is `admin`, `operator`, `auditor`, `viewer`
(matrix below), and the template's personas are **aliased** onto it. The earlier lean
(namespaced roles only, no aliasing) was rejected.

**Alias table.** A bare persona role in `resource_access.<OIDC_CLIENT_ID>.roles` grants the
trustbridge role below **at instance scope**:

| Template persona | Trustbridge role | Why |
| --- | --- | --- |
| `viewer` | `viewer` | same meaning (read-only) |
| `security` | `auditor` | audit read, no change |
| `detective` | `auditor` | reads audit and data, writes neither; exactly an auditor |
| `editor` | `operator` | closest in power: changes things, but not configuration |
| `maintainer` | `admin` | the template's highest write role; its delete is owner-scoped on example data, so this is the loosest fit |

My analysis gave no better mapping for `editor` or `maintainer`: the template's meanings are
about editing records, not operations or configuration, so these two are the weakest matches
and are aliased as the user asked.

**Role names and scope.** The namespaced forms stay and are the way to grant a role at
**tenant scope**: `trustbridge:<role>` (instance) and `trustbridge:<host>:<org>:<role>` (one
tenant), user decision 4. Personas are bare names, so they can only grant instance scope. A
principal's roles are the union of its namespaced roles and its aliased personas.

**Matrix and "Min role".** The effective permissions form a ladder plus one branch: `viewer`
reads operational data except audit; `operator` includes `viewer` and adds requeue, reconcile,
rotate and registration start; `admin` includes `operator` and adds registration upload,
tenant disable and enable, and tenant data deletion (0034); `auditor` includes `viewer` and
adds audit list and export, and mutates nothing. "Min role" in the endpoint tables means that
role or any role that includes it, so an `operator` reads without a separate `viewer` grant.
**Audit is the exception to the ladder:** only `auditor` reads it, so an `admin` or `operator`
gets 403 on audit unless they also hold `auditor`. Separation of duties: the person who
changes things is not the person who reads the audit; a user needing both holds both.

**Accepted consequence:** a template persona grant carries production privilege in
trustbridge, for example a `maintainer` is an instance `admin` who can disable and delete
tenants, and the dev realm's persona users are therefore trustbridge users. Personas must be
assigned deliberately; a deployment that does not want this removes the persona roles from the
client or does not assign them. The alias can be switched off by a setting
(`RBAC_ALIAS_TEMPLATE_PERSONAS`, default on) if the user wants a deployment to be namespaced-only.
Because the personas already exist in the devcontainer realm, the dev users work against the
admin API without a realm change (only the tenant-scoped roles need new realm entries).

**Still a template question, not decided here:** the Hero CRUD and `/audit` example routes stay
mounted in the same app; whether production disables them is open (Open issues).

Sources: EB-2; NFR-0011, NFR-0014; FR-0015, FR-0016; ADR 0011; Kubernetes RBAC good practices;
NIST SP 800-53 AC-5, AC-6.

## Invariants the implementation must test

Each is one testable sentence; NG6b, NG6d and NG6e implement and test them.

**Authentication and authorization**

1. A test walks the admin router and fails for any route with no role requirement, and for
   any route whose dependencies are not, in order, authentication, the rate limit and the role
   check (NFR-0011, NFR-0012).
2. A request with no token or an invalid token gets 401 problem details with the RFC 9728
   `WWW-Authenticate` challenge, and the error body never names the validation detail outside
   `MODE=dev`.
3. A valid token with no role, or a role that is not enough for the route, gets 403 problem
   details and an `authz.denied` audit event (0032) carrying its `sub`.
4. Roles come only from `resource_access.<OIDC_CLIENT_ID>.roles`; a role name that is not a
   template persona, `trustbridge:<role>` or `trustbridge:<host>:<org>:<role>` with a known
   role grants nothing, each persona grants exactly its aliased role at instance scope
   (security and detective: auditor; viewer: viewer; editor: operator; maintainer: admin), a
   role naming an org
   with no tenant grants nothing, and a token without `resource_access` yields a principal
   with no roles, not an error.
5. A route with `{tenant_id}` answers 404, the same body as for an unknown tenant, to a
   principal with no grant on that tenant, never reveals whether the tenant exists, and writes
   an `authz.denied` event with a null `tenant_id` and the requested id in `resource_id` (0032).
6. The tenant context is set only after the role check passes, and a handler that is reached
   sees only its tenant's rows (0031 invariants 1 and 2, exercised through the API).
7. An instance-scoped endpoint rejects a tenant-scoped grant, and a tenant-scoped grant on
   tenant A confers nothing on tenant B.
8. Nothing in the admin API reads or writes `store:tenants/<uuid>/<adapter>`'s values: the
   adapters endpoint returns only the name, the configured flag and capabilities, and a test
   plants a canary connection value and asserts it appears in no response.

**Contract**

9. Every response that is an error is `application/problem+json` with `type`, `title`,
   `status`, `detail` and `instance`, including 401, 403, 404, 409, 422, 429 and 500 (ADR
   0004).
10. Every list endpoint returns the pagination envelope, honours `limit` up to its maximum,
    rejects a larger or non-positive `limit` with 422, and a cursor resumes without repeating
    or skipping a row when rows are inserted between pages.
11. The generated OpenAPI spec of the admin router equals the committed snapshot; a route
    change without a snapshot update fails CI; the spec contains no template route.
12. Adding a route under v1 does not change an existing route's schema, and a breaking change
    exists only under a new version prefix (reviewed against the snapshot diff).
13. No admin route is reachable at an unversioned path; the router walk finds none outside
    `/admin/v1`.

**Rate limits**

14. A request over its bucket gets 429 problem details with `Retry-After` of the seconds left
    in the window (at least 1), reads and mutations count in separate buckets, the key is the
    principal after authentication and the source address before it (a 401 counts against the
    address; a 403 or 404 for an authenticated principal against that principal's read bucket),
    and an exempt address is never limited.
15. When Valkey errors or exceeds the limiter's timeout, an admin request is served (fail
    open) and a metric increments, as for the webhook route (0028 invariant 28).
16. Two api replicas sharing one Valkey share each principal's count.

**Mutations**

17. Every mutating endpoint is audited in the same transaction as its change (0032
    invariants 8 and 9), is idempotent where the verb says so (requeue of an already
    deliverable row is a no-op success), and rejects a request body over its bound.
18. The registration start endpoint returns no secret, and the manual registration endpoint
    accepts the PEM and webhook secret only over a request that the audit event records
    without them.
19. `_sync` and `tb_upsert_tenant` never change the admin-disable columns, a disabled tenant
    starts no adapter operation other than a deletion's own delete and revoke operations (0034), its webhooks are stored and not handled until it is enabled,
    `enable` makes them deliverable again, and enable and disable need the `admin` role at
    instance scope; a tenant with a non-terminal deletion cannot be enabled.
19a. With `RBAC_ALIAS_TEMPLATE_PERSONAS` on, each persona grants exactly its aliased role at
    instance scope; with it off, a persona grants nothing in the admin API and only the
    namespaced roles count.
20. A renamed organisation (`tenants.org` updated by `_sync`) stops matching its old
    tenant-scoped role names immediately and grants nothing until the role is renamed.
21. Every mounted admin route is under `/admin/v1`, and no admin route exists at an unversioned
    path or under the template's `/crud`, `/audit` or `/mock`.

## Open issues

- **Policies endpoint.** The UI's "policies" screen has no backing table or ADR; not part of
  v1 unless added as a separate item.
- **Where the limiter lives.** Moving the counter and fail-open core from
  `trustbridge.webhooks` to a layer both can import changes the import-linter contract
  (`main` > `webhooks` > `worker` > `settings`), a NG6 decision inside its write set.
- **BFF in front.** Behind the BFF all users share its address; the principal key handles
  rate limits, but audit's `source_ip` then records the BFF's address unless the BFF forwards
  the client address and `TRUSTED_PROXIES` lists it (0028). The Helm chart (NG7) and the BFF
  plan must agree.
- **Spec path under a sub-mount.** If a sub-application is used instead of a router, handlers
  (Problem Details, the exception handlers, middleware) must be registered on it too; the
  router-based spec avoids this and is the plan.
- **Role token size.** Many per-tenant client roles can exceed header limits at the BFF or
  the ingress; "Full scope allowed" off and instance scope for wide access are the remedies
  (user decision 4). A DB mapping table, rejected for now, would remove the risk.
- **Template example routes in production.** The Hero CRUD and `/audit` stay mounted in the
  same app; whether a production deployment disables them is a template-level question.
- **Dev realm.** The persona users already exist and work through the aliases; adding the
  tenant-scoped trustbridge roles to the devcontainer realm export is a `.devcontainer/`
  change that needs the user and a rebuild.
- **CORS.** The API is called by the BFF server-side; no CORS is added for v1.
- **Idempotency keys** for POSTs are not part of v1; requeue and enable/disable are idempotent
  by nature and rotate is serialised by the lease (0028).

## Consequences

- One versioned, documented, tested surface for the UI and for operators, with authorization
  decided in a single dependency and checked by a router walk, so a forgotten check fails CI.
- A snapshot-checked spec makes every API change a reviewed diff, at the cost of a regenerate
  step on each route change.
- Deny-by-default, 404 for foreign tenants and a Problem Details body everywhere make the API
  uniform but give less information to a mistaken caller, which is intended.
- Keeping connection data operator-only means the UI shows only "configured yes/no" for a
  platform connection until a reviewed writer exists, so NG8's "platform connections" screen
  is read-only at first.
- Rate limits keyed on the principal depend on the BFF forwarding the user's token or a
  per-user identity; with a shared service token every user would share one bucket (the BFF
  plan must pass the user's token through, ADR 0026).
- The role matrix and the role-name convention become an operator-facing contract; changing
  them is a documented migration.
- Keycloak-shaped role claims (user decision 3) make the API not portable to an IdP that does
  not emit `resource_access`, unless a broker or mapper writes it (NFR-0014 trade-off).
- A role per tenant and role per user is administrative work in Keycloak and is not created by
  trustbridge when a tenant is installed.
- Aliasing makes a template persona grant carry production privilege (decision 2); personas
  must be assigned deliberately.
- Tenant deletion (0034) is an irreversible admin action available from the API.

## Sources

- EB-1, EB-2, EB-3, EB-4, EB-11, EB-15 (the plan's "Enterprise baseline" rows).
- Template ADRs [0002](0002-api-and-model-versioning.md),
  [0003](0003-auth-strategy-and-federated-backends.md),
  [0004](0004-uniform-problem-details-error-responses.md), 0008, 0013,
  [0026](0026-resource-server-metadata-and-bff.md); NFR-0001, NFR-0002, NFR-0011, NFR-0012,
  NFR-0014, NFR-0015 and NFR-0027 (`docs/nfrs/`).
- RFC 9457 (Problem Details): https://www.rfc-editor.org/rfc/rfc9457
- RFC 6585 section 4 (429) and RFC 9110 (`Retry-After`):
  https://www.rfc-editor.org/rfc/rfc6585, https://www.rfc-editor.org/rfc/rfc9110
- IETF `RateLimit` header fields draft:
  https://datatracker.ietf.org/doc/draft-ietf-httpapi-ratelimit-headers/
- Google AIP-158 (pagination) and AIP-185 (versioning):
  https://google.aip.dev/158, https://google.aip.dev/185
- OpenAPI 3.1: https://spec.openapis.org/oas/v3.1.0; FastAPI OpenAPI docs:
  https://fastapi.tiangolo.com/how-to/extending-openapi/
- Kubernetes RBAC good practices and API conventions:
  https://kubernetes.io/docs/concepts/security/rbac-good-practices/,
  https://github.com/kubernetes/community/blob/master/contributors/devel/sig-architecture/api-conventions.md
- NIST SP 800-53 Rev. 5, AC-5 (separation of duties):
  https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final
- Keycloak server administration guide (client roles, group membership mapper):
  https://www.keycloak.org/docs/latest/server_admin/
- GitHub App best practices:
  https://docs.github.com/en/apps/creating-github-apps/about-creating-github-apps/best-practices-for-creating-a-github-app
- ADRs [0028](0028-trustbridge-architecture-and-instance-layout.md),
  [0030](0030-key-signer-and-github-hosts.md),
  [0031](0031-tenancy-and-row-level-security.md), [0032](0032-audit-log.md).
- Code read: `src/app/oidc.py`, `src/app/rate_limit.py`, `src/app/main.py`,
  `src/app/controllers/audit.py`, `src/trustbridge/webhooks/limiter.py`,
  `src/trustbridge/github/manifest.py`, `src/trustbridge/worker/schema.py`.
