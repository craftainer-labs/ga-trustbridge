# 0033. Admin API v1: base path, roles, tenant scoping, rate limits and the OpenAPI contract

## Status

Proposed. For the user's G-api review (NG6a0); no code is written yet. Nine items are open for
the user (see "Decision needed"); the rest is design that follows existing ADRs and NFRs. It
becomes Accepted when the implementation (NG6b RBAC, NG6d read endpoints, NG6e mutating
endpoints) merges. Builds on
[0028](0028-trustbridge-architecture-and-instance-layout.md) (instance layout, the abuse
limiter, the NG6 split of limits),
[0030](0030-key-signer-and-github-hosts.md) (host state, registration),
[0031](0031-tenancy-and-row-level-security.md) (tenant context) and
[0032](0032-audit-log.md) (what is audited). Inherits [0004](0004-uniform-problem-details-error-responses.md)
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
- **Tenant disable, if added, is a separate column** that the installation `_sync` never
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

### Roles

The role set is Decision needed 2. Whichever set is chosen is checked in one place, the RBAC
dependency (`trustbridge.rbac`), which turns the token into a `Principal`: `sub`, `iss`, the
roles with their scopes. Role names are namespaced by a prefix (default `trustbridge`) so they
do not collide with the template's personas.

### Tenant disable

See Decision needed 8. If added: a tenant row gets `admin_disabled_at timestamptz null`,
`admin_disabled_by` (the actor's `sub`) and `admin_disable_reason`, all separate from `state`.
`tb_upsert_tenant` and `_sync` (0031) never write them. A tenant is **effective-active** only
when `state = 'active'` and `admin_disabled_at IS NULL`. A disabled tenant: its webhooks are
stored and acknowledged but their handlers are skipped (left unprocessed, like a delivery
waiting for a host that is not ready, 0028 item 3, and re-relayed when it is enabled again);
no new adapter operation starts; in-flight operations finish or are abandoned by the normal
rules; the admin API still reads it. It is reversible (`enable`), audited, and requires the
`admin` role at instance scope.

## Decision needed

For each item: the options, their trade-offs, the sources, and a lean that is only a lean. The
numbering matches the NG6a0 brief.

### 1. Admin API base path

Fixed points: the manifest callback is `/admin/github/registration/callback` (a browser redirect,
not an API call; its URL is generated per flow, but the constant exists); the template owns
`/crud/vN`, `/audit` and `/mock`; webhooks are `/webhooks/...`; NFR-0001 says every resource is
under an explicit `/vN` prefix with no unversioned alias.

| Option | Example | For | Against |
| --- | --- | --- | --- |
| A. `/v1/...` | `/v1/tenants` | Literally NFR-0001; shortest | Collides conceptually with the template's `/crud/v1`; no path prefix to route or expose admin traffic apart from `/webhooks`; the callback `/admin/...` sits outside the versioned tree |
| B. `/admin/v1/...` | `/admin/v1/tenants` | One `/admin` prefix: an ingress can expose `/webhooks` publicly and `/admin` only internally (a real EB-10/EB-5 concern); the callback already lives under `/admin/` | NFR-0001 must be read as "a `/vN` segment" or amended (NG6g can clarify the NFR); one more segment |
| C. `/api/v1/...` | `/api/v1/tenants` | A common convention | The template already uses `/api/_crud` for something else; no gain over A or B |

Lean: **B**, for the ingress separation and because the callback is already under `/admin`,
with NG6g amending NFR-0001's wording if the user accepts. Sources: NFR-0001 and
ADR 0002; `app/main.py:212-242`; `github/manifest.py:48`; Kubernetes API conventions (versioned
group paths); Google AIP-185 (major version in the path).

### 2. Roles

- **A. Four roles: `admin`, `operator`, `auditor`, `viewer`.** `viewer` reads the operational
  data except audit; `auditor` reads everything including audit and the export and mutates
  nothing; `operator` does operations (requeue a delivery, trigger a reconcile, rotate a
  credential) and reads like a viewer; `admin` does configuration (registration, tenant
  enable/disable, role mappings if stored) and everything else. Separation of duties: the person
  who operates is not the person who audits, and an auditor cannot change what they audit.
- **B. Three roles: `admin`, `editor`, `viewer`.** Fewer to learn and to map; the audit log is
  readable by `admin` only, so an auditor who must not change anything cannot exist.
- **C. The template's personas** (`security`, `detective`, ...). Free, but they are
  example names with no meaning for this domain.

Permissions (matrix for A; rows are the endpoint groups in "Endpoints"): `viewer`: read
endpoints except audit. `auditor`: viewer plus audit list and export. `operator`: viewer plus
requeue, reconcile, rotate, registration start. `admin`: operator plus registration upload,
tenant disable/enable, mapping management. Roles are not hierarchical in the token: a user
needing two roles is in two groups, which keeps each grant explicit and auditable.

Lean: **A**, for separation of duties (EB-2, EB-4: an audit log is weaker if the audited can
also read it exclusively). Sources: EB-2; Kubernetes RBAC good practices; NIST SP 800-53 AC-5
(separation of duties); NFR-0011.

### 3. IdP groups to roles

- **A. Keycloak client roles**: keep `resource_access.<client>.roles`, the template's
  `require_roles` path. Zero new code; but NFR-0014 states that shape is Keycloak-specific, and
  EB-1 calls for SAML through an IdP broker, where the broker normalises groups, not
  Keycloak client roles, depending on how it is set up.
- **B. A configurable groups claim plus a mapping setting**: `RBAC_GROUPS_CLAIM` (default
  `groups`) and a mapping (`RBAC_GROUP_MAPPING`, or a naming convention, see item 4) from group
  to role and scope. Works with any IdP that can emit a groups claim (Keycloak's group
  membership mapper, Entra, Okta); costs a mapper in the IdP, a normaliser (Keycloak emits a
  leading `/` unless "full group path" is off) and a bounded token size when a user has many
  groups. The client-role path can be offered as a second source behind the same mapper.
- **C. Both**, A as the default source and B opt-in.

Lean: **B**, with A kept as a fallback source in the same `Principal` builder (so a Keycloak-
only deployment needs no mapper change), because EB-2 says "roles mapped from IdP groups" and
NFR-0014 forbids assuming the Keycloak shape. Sources: EB-1, EB-2, NFR-0014, ADR 0003;
Keycloak server administration guide (group membership mapper, client scope);
OIDC Core claims.

### 4. How users are tied to tenants

- **A. Per-tenant group names**: instance roles are the group `trustbridge:<role>`; a tenant
  role is `trustbridge:<host>:<org>:<role>`, with `<host>` the `GITHUB_HOSTS` name and `<org>`
  the organisation login. The IdP is the only source of truth (EB-2), no extra state, and the
  mapping is visible in the IdP. Costs: a group per tenant and role (many groups for many
  tenants, and a larger token); an org login can be renamed on GitHub, which breaks the group
  until an admin renames it (the tenant row keeps the durable `org_id`, but the group name
  carries the login, which EB-1 warns against for people, less so for an org); admins of the
  IdP, not of trustbridge, decide who sees a tenant.
- **A'. The same with the numeric org id** in the name (`trustbridge:<host>:<org_id>:<role>`):
  rename-proof, but nobody remembers an id.
- **B. A database mapping table** `tenant_members(tenant_id, subject_type, subject, role)`
  where the subject is a group or a `sub`, managed through the API by `admin` (audited,
  self-service, a role is granted per tenant without touching the IdP). Costs: a second source
  of truth next to the IdP, a bootstrap (an instance-scope admin group from the IdP must exist
  to create the first mapping), more endpoints and an RLS-covered table (an extra tenant-owned
  table, 0031), and drift when someone leaves the IdP group but stays in the table (a group
  subject mitigates it; a `sub` subject does not).
- **C. Hybrid:** instance roles from IdP groups, tenant roles from the table.

Lean: **A**, because it keeps the IdP the single source of truth for who may do what and needs
no new state; move to B or C if the group count or the rename problem bites. Sources: EB-1,
EB-2; Kubernetes RBAC good practices; GitHub App best practices (installation = tenant, ids not
handles); Keycloak documentation.

### 5. Rate-limit defaults

The policy is the API's own (0028). The mechanism is NG3's Valkey fixed-window limiter, reused
for a different key prefix (`tb:rl:admin:`); it fails open on a Valkey outage like the webhook
route. Because that limiter lives in `trustbridge.webhooks`, reusing it means moving its
counter and fail-open core up a layer (a NG6 refactor inside its own write set).

- **A. Proposed:** keyed on the authenticated principal (`sub`) after authentication, on the
  source address before it (so a failed-token flood is bounded too);
  **600 requests/minute per principal** for reads and a separate, lower bucket of
  **60/minute per principal** for mutating calls and **30/minute per source address** for
  unauthenticated or rejected requests; fixed window; 429 Problem Details with `Retry-After`
  (whole seconds left in the window, at least 1) and the IETF `RateLimit-*` headers on all
  responses as an option; exports have their own small bucket (for example 6/minute) because
  they are long reads. All are settings (`ADMIN_RATE_LIMIT_*`).
- **B. The template's slowapi-style values** (`10/minute` for the token endpoint, `20/minute`
  for bulk actions, per route and client address): tuned for a template's destructive routes,
  too low for a UI that loads several lists per screen behind a BFF where many users share
  one source address, and per-address keys would throttle all users behind the BFF together.
- **C. No per-principal limit in the app**, rely on the ingress: consistent with 0028's
  rejected option for webhooks (the user chose the application limiter).

Lean: **A**, on the principal rather than the address, because the BFF funnels many users
through one address. The numbers are guesses to be measured with the UI; invariant 14 only
fixes their behaviour. Sources: EB-11; RFC 6585 section 4 (429); RFC 9110 (`Retry-After`);
IETF draft `ratelimit-headers`; 0028 "Abuse limits on the webhook route"; `app/rate_limit.py`.

### 6. OpenAPI v1: pagination, how the spec is served, endpoints

**Pagination.**

- **A. Cursor** (keyset): `limit` (default 50, max 200) and an opaque `cursor`; the response is
  `{ "items": [...], "next_cursor": "..." | null }`. Stable while rows are inserted (audit
  and deliveries grow constantly), constant cost at depth, but no jump to page N and no
  total count (an optional `total` may be offered for small collections).
- **B. Offset**: `skip`/`limit` like the template's CRUD (ADR 0008). Simple, supports page
  numbers in a UI table, matches the template's generic client code; unstable under inserts
  (rows shift between pages) and O(n) at depth, which hurts the audit table.
- **C. Cursor for audit and deliveries (growing, append-heavy), offset for small tables**
  (hosts, tenants, adapters): two conventions in one API.

Lean: **A** for every list, for one convention and a stable audit view; the UI loads "next
page" rather than numbered pages. Sources: Google AIP-158 (pagination), Stripe and GitHub API
pagination, Postgres keyset pagination; ADR 0008.

**How the spec is served.**

- **A. Served at `/admin/v1/openapi.json`** (or under the chosen base), generated from the
  admin router only (not the template's `/crud` routes), and **also a committed snapshot**
  (`docs/openapi/admin-v1.json`) that a CI test compares with the generated spec, so any change
  is a reviewed diff. Serving needs any authenticated role, not public.
- **B. Snapshot only**: nothing served at runtime, which is less surface and works for an
  air-gapped install where the file ships with the release; the UI and clients are generated
  from the file.
- **C. Served only**, no snapshot: no drift test, no reviewed contract.

The mounted sub-spec is built from the admin router (FastAPI `get_openapi` with that router's
routes) so the template's example routes do not leak into the contract, and so the spec carries
the Problem Details schema, the pagination envelope and the security scheme.

Lean: **A** (served, authenticated, plus a CI-checked snapshot attached to each release), so
there is a reviewed contract and a way for tools to read it in place. Sources: EB-11;
FastAPI OpenAPI docs; OpenAPI 3.1; NFR-0002.

**Endpoint list.** Base path per item 1; `{t}` is a tenant id. Roles per item 2.

Read-only (NG6d):

| Endpoint | Scope | Min role |
| --- | --- | --- |
| `GET /me` (the principal: `sub`, roles, tenants) | any | any |
| `GET /hosts`, `GET /hosts/{host}` (state, reason, answering replica; 0030) | instance | viewer |
| `GET /tenants`, `GET /tenants/{t}` (the effective state, the two state columns) | per grants | viewer |
| `GET /tenants/{t}/repositories` | tenant | viewer |
| `GET /tenants/{t}/adapters` (name, configured yes/no, capabilities; never connection data) | tenant | viewer |
| `GET /tenants/{t}/resources`, `/credentials` (state and timestamps, never a value), `/operations` | tenant | viewer |
| `GET /deliveries`, `GET /deliveries/{host}/{id}` (status, attempts, error; body only for `operator`) | instance | viewer |
| `GET /audit-events`, `GET /audit-events/{id}` | per grants | auditor |
| `GET /audit-events/export` (NDJSON, 0032) | per grants | auditor |
| `GET /openapi.json` | any | any |

Mutating (NG6e):

| Endpoint | Scope | Min role |
| --- | --- | --- |
| `POST /deliveries/{host}/{id}/requeue` | instance | operator |
| `POST /tenants/{t}/reconcile` | tenant | operator |
| `POST /tenants/{t}/credentials/{id}/rotate` | tenant | operator |
| `POST /hosts/{host}/registration` (start the manifest flow; `CALLBACK_PATH` stays the browser callback), `POST /hosts/{host}/registration/manual` (upload App id, PEM, webhook secret, 0030) | instance | admin |
| `POST /tenants/{t}/disable`, `POST /tenants/{t}/enable` (only if item 8 says yes) | instance | admin |
| mapping endpoints (only if item 4 chooses a table) | tenant | admin |

Not in v1 by design: any write of adapter connection data (decided), a policies endpoint
(there is no policy table yet; the UI's "policies" screen needs one, which is a separate
item and ADR), and tenant data deletion (item 9).

### 7. Audit: transport, tamper evidence, authn failures

These three are decided on in [0032](0032-audit-log.md) "Decision needed" (authn failures,
export transport, tamper evidence), with the same labelled leans: NDJSON pull plus the list
endpoint; grants plus trigger and function-only purge, no hash chain; authn rejections
logged but not stored, authorization denials stored. The user answers them there.

### 8. Tenant disable in NG6 or not

No EB row requires it.

- **A. Not in NG6.** Less surface; an operator who needs to stop a tenant suspends the
  installation on GitHub (which `_sync` then mirrors) or removes its connection data
  (operator-only already).
- **B. In NG6** as designed under "Tenant disable": a column, a worker and reconciler check,
  two endpoints, six or so invariants. A kill switch for an incident (a tenant with
  compromised credentials) that does not depend on GitHub-side action or on touching the
  secret store.

Lean: **B**, because it is small, security-relevant and cheaper before the worker's tenant
checks are widely spread than after. Sources: the NG34f note on `_sync`; EB-3; OWASP ASVS
(account suspension) is not directly applicable (tenants are not accounts), so this is a
judgement.

### 9. EB-15 tenant data deletion

- **A. In NG6**: `DELETE /tenants/{t}/data` requiring the tenant to be disabled first, purging
  tenant rows. Open questions it drags in: platform-side resources and credentials (delete or
  leave?), parked secret values, the audit rows (0032 interplay), GitHub-side
  secrets and variables, and confirmation semantics for an irreversible call.
- **B. A follow-up** after NG6: NG6 ships audit retention (0032) and the existing webhook body
  retention only; deletion gets its own ADR once the platform-side semantics are settled.

Lean: **B**, because the hard part is the platform-side semantics, not the SQL, and an
irreversible endpoint deserves its own review. Sources: EB-15; 0028 "Retention and trimming";
0029 (adapter `delete` semantics).

## Invariants the implementation must test

Each is one testable sentence; NG6b, NG6d and NG6e implement and test them. Items that depend
on a Decision needed outcome say so.

**Authentication and authorization**

1. A test walks the admin router and fails for any route with no role requirement, and for
   any route whose first dependency is not authentication (NFR-0011, NFR-0012).
2. A request with no token or an invalid token gets 401 problem details with the RFC 9728
   `WWW-Authenticate` challenge, and the error body never names the validation detail outside
   `MODE=dev`.
3. A valid token with no role, or a role that is not enough for the route, gets 403 problem
   details and an `authz.denied` audit event (0032) carrying its `sub`.
4. Roles come only from the configured source (item 3) and a role claim a token carries that
   is not in the mapping grants nothing; a token whose groups claim is absent or malformed
   yields a principal with no roles, not an error.
5. A route with `{tenant_id}` answers 404, the same body as for an unknown tenant, to a
   principal with no grant on that tenant, and never reveals whether the tenant exists.
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
    or skipping a row when rows are inserted between pages (under the leaned option).
11. The generated OpenAPI spec of the admin router equals the committed snapshot; a route
    change without a snapshot update fails CI; the spec contains no template route.
12. Adding a route under v1 does not change an existing route's schema, and a breaking change
    exists only under a new version prefix (reviewed against the snapshot diff).
13. No admin route is reachable at an unversioned path (NFR-0001).

**Rate limits**

14. A request over its bucket gets 429 problem details with `Retry-After` of the seconds left
    in the window (at least 1), reads and mutations count in separate buckets, the key is the
    principal after authentication and the source address before it, and an exempt address is
    never limited.
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
19. If tenant disable is accepted (item 8): `_sync` and `tb_upsert_tenant` never change the
    admin-disable columns, a disabled tenant starts no adapter operation, its webhooks are
    stored and not handled until it is enabled, and `enable` makes them deliverable again;
    enable and disable need the `admin` role at instance scope.
20. If a mapping table is accepted (item 4): it is tenant-owned and RLS-covered, an `admin`
    can grant only roles within their own tenant, and the last instance admin cannot be
    removed through the API.

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
- **Group token size.** Many per-tenant groups can exceed header limits at the BFF or the
  ingress; item 4's lean has this risk and the table option removes it.
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
- The role matrix and group conventions become an operator-facing contract; changing them is
  a documented migration, which is why they are put to the user now.

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
