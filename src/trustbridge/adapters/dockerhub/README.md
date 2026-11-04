# adapters/dockerhub/

The Docker Hub adapter (NG5c, [ADR 0029](../../../../docs/adrs/0029-adapter-plugin-interface.md)).
Entry point `dockerhub = "trustbridge.adapters.dockerhub:DockerHubAdapter"` in the
`trustbridge.adapters` group.

- `adapter.py` — `DockerHubAdapter` (all `PlatformAdapter` methods) and the grant mapping.
- `client.py` — the httpx client: it exchanges the PAT for a short-lived bearer (`POST
  /v2/auth/token`) on first use, then sends that bearer. Status-to-`AdapterError` mapping (408,
  425, 429, 5xx and transport errors retry; other statuses do not). No response body or header is
  ever put into an error message.

Connection data per tenant (`AdapterConfig.connection`): `namespace` (required: the Docker Hub
organization; it is the "project"), `username` (required: the dedicated service user that owns
the PAT) and an optional `base_url` (https, no query; default `https://hub.docker.com`). The
organization-owner PAT is `privileged_credential`. The adapter is stateless;
`DockerHubAdapter(config, *, transport=None, clock=None)` takes the last two for the contract
tests. `github_outputs` reads the constant per-instance `namespace` (user decision: that counts as
pure); the constructor therefore rejects a missing or empty `namespace` and a bad `base_url`
(`AdapterConfigurationError`), so `github_outputs` cannot raise. The username and the PAT are
checked per call. The namespace is also the `DOCKERHUB_USERNAME` variable because Docker documents
`docker login --username <YOUR_ORGANIZATION_NAME>` for an organization access token (the
organization access tokens page below) and the login API's `identifier` as "an organization name"
for one.

`base_url` is operator configuration and is not checked against an allow-list. No tenant or
webhook input reaches it: the only writer of connection data, `ConnectionData.put`
(`worker/ports.py`), has one caller (`github/manifest.py`) that writes fixed keys to the `argocd`
entry; the `dockerhub` entry is provisioned in the secret store out of band and read back by
`config_for`. Any future admin-API writer of connection data needs the same SSRF review (the PAT is
sent to that URL).

## What it provisions

| Interface method | Docker Hub call |
| --- | --- |
| login (every call) | `POST /v2/auth/token` `{identifier: username, secret: PAT}` returns `access_token` (valid 10 minutes); a 401 mid-operation logs in once more and retries that request once, a second 401 is a non-retryable error |
| `ensure_project` | `GET /v2/orgs/{ns}/settings` (the documented organization lookup: 200 exists, 404 unknown); creates nothing. 404 is a non-retryable "not found"; 403 and every other status are errors, never "present" |
| `ensure_repository` | `GET /v2/namespaces/{ns}/repositories/{name}`, `POST /v2/namespaces/{ns}/repositories` (`name`, `namespace`, `description`, `is_private: true`); an existing repository must carry the marker and be private |
| `ensure_webhook` | none: returns `NEEDS_MANUAL_ACTION` (see Decisions) |
| `delete` | none: a repository is `NEEDS_MANUAL_ACTION`, a namespace or webhook is `UNCHANGED` |
| `ensure_credential`, `rotate_begin` | `GET /v2/orgs/{ns}/access-tokens` (paged), `POST /v2/orgs/{ns}/access-tokens`, then `DELETE .../{id}` of older same-label tokens; `GET`/`PATCH .../{id}` of the recorded token |
| `rotate_finish`, `delete_credential` | the same listing, then `DELETE` of the tokens whose label is the slot name |
| `list_credentials` | `GET /v2/orgs/{ns}/access-tokens`, label prefix `tb-<tenant hex>-` |
| `health_check` | login plus `GET /v2/namespaces/{ns}/repositories?page_size=1` |

## Grants: per repository or per namespace

Docker's organization access tokens (OATs) carry `resources`, each a `TYPE_REPO` path with its own
scopes, so Docker Hub **can** narrow to one repository (personal access tokens cannot: Docker
states that a PAT's repository permission "applies to every repository your account can access").
The adapter therefore always issues OATs, never PATs.

- `CredentialScope.platform_repository_key` set: the resource path is `<namespace>/<key>`, one
  repository.
- Unset: the path is the glob `<namespace>/*`, every repository of the namespace; the claims narrow
  who can obtain it. Docker documents `myorg/*` as "all repositories in the organization".
- Scopes: pull `scope-image-pull`; push `scope-image-push`; admin `scope-image-delete` and
  `scope-repository-edit`. Each scope also grants the lesser ones it includes. The adapter never
  asks for `scope-repository-admin` (it deletes the repository), `scope-repository-create` (an
  organization scope), or any organization, webhook or product scope.
- A repository key must match Docker's repository-name pattern, so it can never carry a glob.
- A delivered token's resources are read back on each reconcile and `PATCH`ed when they drifted
  (for example when `platform_repository_key` changed); the secret is not re-issued.

## Decisions

- **Privileged credential (ADR 0029 item 7).** The OAT endpoints under `/v2/orgs/{org}/access-tokens`
  are called with a bearer from the tenant's organization-owner PAT. An OAT cannot create OATs
  (ADR 0029, NR5), so this long-lived owner credential is the accepted exception to EB-9.
- **Ownership marker (never adopt or delete foreign objects).** Every token the adapter creates has
  the label `tb-<tenant uuid hex>-<scope key>-g<generation>` and description `managed by
  trustbridge`. Token ids are checked against the UUID format Docker's examples show before they
  are used in a URL; any other id is refused. The adapter lists, confirms and deletes tokens only by an exact label that starts
  with the tenant's prefix, and refuses (non-retryable) a slot name without it; humans' tokens and
  other tenants' tokens in the same organization are never touched. A repository is created with
  the description `managed by trustbridge`; an existing repository without that description is a
  non-retryable error, not adopted. Repositories are always created private; an existing marked repository that is public is a
  non-retryable error naming the manual fix (set it private in the repository's Settings), because
  the adapter never changes visibility.
- **Two-phase rotation.** `rotate_begin` creates the next generation's OAT under its own label while
  the previous one stays valid; `rotate_finish` deletes the previous. Issuing is itself two-phase:
  the OAT is created first, and only then are the older same-label tokens deleted (their secret is
  gone, Docker shows it once, and they count toward the limit). A failed create leaves the old
  tokens in place and is retried. If a delete fails after the create, the error is retryable and
  the retry finds the new token (its secret was never returned) among the old ones and replaces it,
  so a retry converges to one token. A delivered token is confirmed only by the id its slot
  records; if that token is gone or unusable, a new one is issued with `delivered=False` and the
  core delivers the new secret.
- **Token limit.** `credential_limit=10` (Docker Team; Business allows 100). Expired and deactivated
  tokens count until deleted, so a deactivated or expired delivered token is deleted and re-issued.
  An OAT is created with a 90-day expiry (Docker allows up to one year).
- **Webhooks (NEEDS_MANUAL_ACTION).** Docker's OpenAPI reference lists no webhook endpoints and the
  webhook page documents only the UI, although OATs have `scope-webhook-*` scopes and the access
  token page lists "webhook pipeline settings" among the endpoint groups an OAT can call. The
  shapes are not in primary documentation, so the adapter does not guess: `ensure_webhook` reports
  `NEEDS_MANUAL_ACTION` with the instruction to add the webhook in the repository's Webhooks tab.
  `WebhookTarget` carries no repository, and Docker Hub webhooks are per repository. Docker Hub
  webhooks carry no signature or header secret, so `capabilities().webhook` is `NONE`,
  trigger-only (ADR 0029 matrix): a delivery only triggers a re-read through the API.
- **Never deleted.** The adapter has no repository delete (the OpenAPI reference has none; the
  legacy path rejects OATs and is undocumented): `delete` of a repository reports
  `NEEDS_MANUAL_ACTION`. The namespace is an organization the adapter does not own; deleting it is
  `UNCHANGED`.
- **OIDC.** None (`oidc=False`): Docker Hub's OIDC connections are UI-only (ADR 0029).

## Sources (Docker documentation, checked via context7 and the OpenAPI document)

- Docker Hub API reference, OpenAPI 2-beta (paths, schemas `createOrgAccessTokenRequest`,
  `createOrgAccessTokenResponse`, `orgAccessTokenResource`, `getOrgAccessTokensResponse`,
  `repo_creation_request`, `AuthCreateTokenResponse`):
  <https://docs.docker.com/reference/api/hub/latest/> and
  <https://docs.docker.com/reference/api/hub/latest.yaml>
- Authentication (`POST /v2/auth/token`, identifier is a username for a PAT or an organization name
  for an OAT, bearer expires in 10 minutes):
  <https://docs.docker.com/reference/api/hub/latest/operations/AuthCreateAccessToken/>
- Organization access tokens (creating, limits of 10 for Team and 100 for Business, expired and
  deactivated tokens count, `docker login` with the organization name, resources per repository up
  to 50): <https://docs.docker.com/security/access-tokens/organization-access-tokens/>
- OAT scopes, the repository glob, supported endpoints, PAT repository permission applying to every
  repository: <https://docs.docker.com/security/access-tokens/reference/>
- Webhooks (UI only; OATs with `scope-webhook-edit` can create them through the API):
  <https://docs.docker.com/docker-hub/repos/manage/webhooks/>
- Source of the pages above: <https://github.com/docker/docs/tree/main/content/manuals/security/access-tokens>

## Still to confirm on a live instance

The fixtures encode these as best-effort shapes; none has a primary-documentation statement:

- The label's length and character limits (the adapter's label is 55 characters) and whether labels
  must be unique within an organization.
- That a repository-scoped OAT with `scope-image-push` alone can push to the repository on the
  registry (docs say the scopes include one another), and that the glob `<ns>/*` works in the
  create API as it does in the UI.
- Whether `GET .../access-tokens/{id}` returns `resources` with the scopes exactly as sent (the
  response may expand included scopes, which would make every reconcile `PATCH`).
- The response to `GET /v2/namespaces/{ns}/repositories/{name}` for an unknown repository (404 is
  assumed) and for `POST` on an existing one (409 is assumed; the OpenAPI lists 400, 401, 403, 404
  and 500).
- `GET /v2/orgs/{ns}/settings` answering 200 to an organization owner's bearer and 404 to an unknown
  organization (the OpenAPI lists 200, 401, 403, 404; it states no owner requirement for GET).
- Whether Docker rejects a second token with an existing label: the issue now creates before it
  deletes, so a uniqueness rule on labels would fail the create (400/409, non-retryable).
- The `page_size` maximum for the token listing (100 requested) and that `next` is null on the last
  page.
- That the PAT bearer can call the organization token endpoints without an extra scope, and that a
  PAT of an owner in an SSO-enforced organization works with `POST /v2/auth/token`.
- A `DELETE` of a token that is in use or already gone (404 is treated as gone).
- The webhook API (none is documented), and whether the description field of a repository can be
  edited by someone else, which would break the ownership marker.

## Known limitations

- A repository the adapter created is never deleted through the API; `delete` reports a manual step.
- No webhook is created through the API (see Decisions).
- A failure after the token was created and before the core stored the secret loses the secret; the
  next call creates another and then deletes the lost one. The create comes first so that a failed
  create never costs the old token, which means re-issuing needs a free slot under the token limit
  (a tenant at the limit gets a non-retryable 403 and keeps its old token).
- Docker documents no way to tell a missing organization from a forbidden one except the status
  codes of `GET /v2/orgs/{ns}/settings`; a namespace that is a user account (not an organization)
  is not supported, since organization access tokens exist only for organizations.
- A repository that is public is never made private by the adapter: the person fixes it in Docker
  Hub and the next reconcile succeeds.
- With 10 scopes (Team) rotation is refused by the core's `credential_limit` rule.
- `ensure_project` does not distinguish two `ProjectSpec` keys: a tenant has one Docker Hub
  namespace, and every project spec maps to it.
