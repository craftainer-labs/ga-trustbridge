# adapters/jfrog/

The JFrog Artifactory adapter (NG5a, [ADR 0029](../../../../docs/adrs/0029-adapter-plugin-interface.md)).
Entry point `jfrog = "trustbridge.adapters.jfrog:JFrogAdapter"` in the `trustbridge.adapters` group.

- `adapter.py` — `JFrogAdapter` (all `PlatformAdapter` methods) and the naming/claim helpers.
- `client.py` — the httpx client: bearer auth with the admin token, status-to-`AdapterError`
  mapping (429, 5xx and transport errors retry; other statuses do not), no response body in
  any error message.

Connection data per tenant: `base_url` (https, no query) in `AdapterConfig.connection`, the admin
access token as `privileged_credential`, optional `project_key` (the project credentials and the
webhook target; default: the tenant's `default` project), optional `environment` (the project
environment repositories join; default `DEV`), and one entry per webhook `secret_ref` holding
the signing secret (the core resolves `store:` references before it builds the config). The
adapter is stateless; `JFrogAdapter(config, *, transport=None, clock=None)` takes the last two
for the contract tests. `github_outputs` reads the constant per-instance `base_url` (user
decision: that counts as pure).

## What it provisions

| Interface method | JFrog call |
| --- | --- |
| `ensure_project` | `GET/POST/PUT /access/api/v1/projects[/{key}]`; key `tb` + 20 hex of `sha256(tenant hex \| spec key)` |
| `ensure_repository` | `GET/PUT /artifactory/api/repositories/{key}`; key `<project key>-<spec key>`, `projectKey` and `environments` set, `rclass: local` |
| `ensure_webhook` | `GET/POST/PUT /event/api/v1/subscriptions[/{key}]`, domain `docker`, handler `use_secret_for_signing: true` |
| `delete` | `DELETE` on the three above; the kind comes from `ref["kind"]`, or from the name when `ref` is empty |
| `ensure_credential` (eligible) | `GET/POST /access/api/v1/oidc/{provider}`, `GET/POST/PUT .../identity_mappings[/{name}]` |
| `ensure_credential`, `rotate_begin` (secret) | `GET/POST /access/api/v1/tokens`, `DELETE /access/api/v1/tokens/{id}` |
| `rotate_finish`, `delete_credential` | `DELETE` of the tokens (found by `description` = slot name) and of the mapping |
| `list_credentials` | `GET /access/api/v1/tokens`, `GET .../identity_mappings` |
| `health_check` | `GET /access/api/v1/system/ping` |

## Grants: per repository or per project

Tokens and identity mappings use JFrog's resource permission scope
`artifact:<target>:<actions>` (Artifactory 7.38 and later), which needs no predefined permission
target.

- `CredentialScope.platform_repository_key` set (the core names the `RepoSpec.key` this
  credential is for): the target is that one repository, `<project key>-<key>`.
- Unset: the target is the ant pattern `<project key>-*`, i.e. every repository of the project;
  the claims narrow who can obtain it.
- Actions: pull `r`, push `r,w`, admin `r,w,d,a,m`.

## Decisions

- **Webhook authentication (ADR 0029 item 10): HMAC.** The handler's `use_secret_for_signing`
  makes JFrog sign the payload with the secret instead of sending it in the header, so
  `capabilities().webhook` is `HMAC`, header `X-JFrog-Event-Auth`, trigger-only.
- **Mapping selection.** JFrog evaluates identity mappings by `priority`, the lowest number first.
  Priorities: admin 10, push 20, pull 30, plus 5 for an org-wide scope. A workflow in a protected
  environment matches the push mapping and the pull mapping; push has the lower number, so it
  gets the push token (`test_mapping_selection_prefers_the_strongest_permission`).
- **Audience** `jfrog-github` for every provider and mapping (`OidcTrust.audience`).
- **Rotation is `single`.** A new token is created first, then every older token with the same
  description is revoked, so a crash leaves at most two and a retry converges to one.
- Providers are shared by all of a tenant's scopes and never deleted by the adapter.

## Sources (JFrog documentation, checked via context7 and the doc pages)

- OIDC integration, identity mapping priority, claims, dynamic scopes:
  <https://docs.jfrog.com/administration/docs/openid-connect-integration>
- Create OIDC configuration (`POST /access/api/v1/oidc`; fields `name`, `issuer_url`, `audience`,
  `description`, `provider_type` `generic`/`GitHub`, `token_issuer`):
  <https://docs.jfrog.com/administration/reference/createoidcconfiguration>
- Identity mappings (`/access/api/v1/oidc/{provider_name}/identity_mappings`, body `name`,
  `description`, `claims`, `token_spec`, `priority`; Get by name answers 404 when absent):
  <https://docs.jfrog.com/administration/reference/createoidcidentitymapping>,
  <https://docs.jfrog.com/administration/reference/getoidcidentitymappingbyname>,
  <https://docs.jfrog.com/administration/reference/getoidcidentitymappings>
- Create token (`POST /access/api/v1/tokens`; `username`, `scope`, `expires_in`, `refreshable`,
  `description`, `audience`; response `token_id`, `access_token`, `expires_in`, `scope`):
  <https://docs.jfrog.com/administration/reference/access-tokens>
- Scope syntax including resource permission scopes `artifact:<repo>:<actions>`, and
  `GET` token list shape `{"tokens": [...]}`:
  <https://docs.jfrog.com/administration/reference/create-scoped-token>,
  <https://docs.jfrog.com/administration/docs/access-tokens>
- Create project (`POST /access/api/v1/projects`; `project_key`, `display_name`, `description`;
  409 when the key exists): <https://docs.jfrog.com/projects/reference/createproject>
- Create repository (`PUT /artifactory/api/repositories/{repoKey}`; `key`, `rclass`, `packageType`,
  `projectKey`, `environments` mandatory with a project, `dockerApiVersion`) and get repository:
  <https://docs.jfrog.com/artifactory/reference/createrepository>,
  <https://docs.jfrog.com/artifactory/reference/getrepositoryconfiguration>
- Webhook subscriptions (`/event/api/v1/subscriptions`; `key` pattern `^[A-Za-z][A-Za-z0-9_\-]+$`,
  `project_key`, `enabled`, `event_filter`, one handler with `secret` and `use_secret_for_signing`;
  docker domain criteria `anyLocal`, `anyRemote`, `repoKeys`, `includePatterns`,
  `excludePatterns`; docker events `pushed`, `deleted`, `promoted`):
  <https://docs.jfrog.com/integrations/reference/createwebhooksubscription>,
  <https://docs.jfrog.com/administration/docs/event-types>
- Field-level payloads of the Terraform providers that call these APIs (used where the reference
  pages list a field only as "object"): <https://github.com/jfrog/terraform-provider-platform>
  (`token_spec.username/scope/audience/expires_in`),
  <https://github.com/jfrog/terraform-provider-artifactory>.

## Still to confirm on a live instance

These have no primary-documentation statement the fixtures could be built from. They are encoded
in the fixtures as best-effort shapes and are the first thing to check against a real instance:

- `repository_id`, `repository_owner_id` and `environment` accepted as identity-mapping claims
  (ADR 0029 open issue), and GHES/GHE.com tokens carrying them.
- Resource permission scopes (`artifact:...`) accepted in an identity mapping's
  `token_spec.scope`. The documentation shows them for access tokens, and shows `groups`, `user`
  and `roles` scopes for mappings; it does not show resource scopes for mappings. This is what the
  per-repository trust grant depends on.
- The fields inside `token_spec` (`username`, `scope`, `audience`, `expires_in`): the reference
  lists the object without its fields; they come from the Terraform provider.
- A transient `username` on a token or mapping that has only a resource scope.
- The response of an unknown repository on `GET /artifactory/api/repositories/{key}` (treated as
  absent on 400 or 404), and the answer to `PUT` for an existing key (assumed 400, and 409).
- The JSON shape of `GET .../identity_mappings` (an array, or an object holding one) and of
  `GET /event/api/v1/subscriptions/{key}`.
- The webhook signature algorithm and header format (HMAC-SHA256 in `X-JFrog-Event-Auth` is the
  expectation; the reference only says `use_secret_for_signing` signs the payload), whether
  `project_key` must be sent for the `docker` domain (sent, as the reference requires it when all
  event types are project scoped), and that a changed secret alone is not detectable (the secret is
  never read back).
- The project key length and character limits (20 hex digits after `tb` assumed to be accepted),
  and that a project must be empty before it can be deleted.
- Token and mapping list pagination (none is handled).
- Which error an unlicensed OIDC call returns; the adapter treats any 4xx as non-retryable.

## Known limitations

Found in review and left as is for this item; each needs a design decision or a live instance.

- A credential that switches between an OIDC mapping and an access token (a scope's `oidc_eligible`
  changes) does not revoke the credential of the previous kind; delete or rotate it by hand.
- Whether `GET /access/api/v1/tokens` still lists an expired token is unconfirmed. If it does, a
  delivered slot whose token expired reports `UNCHANGED` until the next rotation.
- Webhook subscriptions are created in the `docker` domain only, and an existing subscription with
  the same URL and events is not compared on secret, project or criteria, so a rotated signing
  secret is not pushed to it.
- Credentials are scoped to the adapter's configured project key; it must equal the key the
  repositories were ensured under (nothing validates that they agree).
- A failure deleting an old token after the new one was created loses the new secret; retrying
  creates another token.
- `connection.base_url` is operator configuration and is not checked against an allow-list.
