# adapters/jfrog/

The JFrog Artifactory adapter (NG5a, [ADR 0029](../../../../docs/adrs/0029-adapter-plugin-interface.md)).
Entry point `jfrog = "trustbridge.adapters.jfrog:JFrogAdapter"` in the `trustbridge.adapters` group.

- `adapter.py` — `JFrogAdapter` (all `PlatformAdapter` methods) and the naming/claim helpers.
- `client.py` — the httpx client: bearer auth with the admin token, status-to-`AdapterError`
  mapping (429, 5xx and transport errors retry; other statuses do not), no response body in
  any error message.

Connection data per tenant: `base_url` (https, no query) in `AdapterConfig.connection`, the admin
access token as `privileged_credential`, optional `project_key` (the project credentials target),
and one entry per webhook `secret_ref` holding the HMAC secret. The adapter is stateless;
`JFrogAdapter(config, *, transport=None, clock=None)` takes the last two for the contract tests.

## What it provisions

| Interface method | JFrog call |
| --- | --- |
| `ensure_project` | `GET/POST/PUT /access/api/v1/projects[/{key}]`; key `tb` + 20 hex of `sha256(tenant hex \| spec key)` |
| `ensure_repository` | `GET/PUT /artifactory/api/repositories/{key}`; key `<project key>-<spec key>`, `projectKey` set, `rclass: local` |
| `ensure_webhook` | `GET/POST/PUT /event/api/v1/subscriptions[/{key}]`, handler `use_secret_for_signing: true` |
| `delete` | `DELETE` on the three above; the kind comes from `ref["kind"]`, or from the name when `ref` is empty |
| `ensure_credential` (eligible) | `GET/POST /access/api/v1/oidc/{provider}`, `GET/POST/PUT .../identity_mappings[/{name}]` |
| `ensure_credential`, `rotate_begin` (secret) | `GET/POST /access/api/v1/tokens`, `DELETE /access/api/v1/tokens/{id}` |
| `rotate_finish`, `delete_credential` | `DELETE` of the tokens (found by `description` = slot name) and of the mapping |
| `list_credentials` | `GET /access/api/v1/tokens`, `GET .../identity_mappings` |
| `health_check` | `GET /access/api/v1/system/ping` |

## Decisions

- **Webhook authentication (ADR 0029 item 10): HMAC.** `terraform-provider-artifactory` documents
  that a handler `secret` is sent as the `X-JFrog-Event-Auth` header, unless
  `use_secret_for_signing` is true, in which case it signs the payload. The adapter always sets
  it, so `capabilities().webhook` is `HMAC`, header `X-JFrog-Event-Auth`, trigger-only. The digest
  algorithm (HMAC-SHA256) and the header value format are NOT confirmed by those docs.
- **Mapping selection.** JFrog resolves identity mappings by `priority`, lowest number first.
  Priorities: admin 10, push 20, pull 30, plus 5 for an org-wide scope. A workflow in a protected
  environment matches the push mapping and the pull mapping; push has the lower number, so it
  gets the push token. The fixtures and `test_mapping_selection_prefers_the_strongest_permission`
  show it.
- **Audience** `jfrog-github` for every provider and mapping (`OidcTrust.audience`).
- **Roles.** The token scope is `applied-permissions/roles:<project>:<role>` with pull=`Viewer`,
  push=`Developer`, admin=`Project Admin`.
- **Rotation is `single`.** A new token is created first, then every older token with the same
  description is revoked, so a crash leaves at most two and a retry converges to one.
- Providers are shared by all of a tenant's scopes and never deleted by the adapter.

## Sources

- OIDC integration (`POST /access/api/v1/oidc`, token exchange):
  <https://docs.jfrog.com/administration/docs/openid-connect-integration>
- OIDC REST reference: <https://docs.jfrog.com/administration/reference/oidc> (index page only
  when fetched; the shapes below come from the provider source)
- Identity mappings (`/access/api/v1/oidc/{provider_name}/identity_mappings[/{name}]`, fields
  `name`, `description`, `priority`, `claims`, `token_spec{username, scope, audience, expires_in}`,
  `project_key`) and OIDC configuration fields (`provider_type`, `issuer_url`, `audience`,
  `token_issuer`, `organization`):
  <https://github.com/jfrog/terraform-provider-platform> (`oidc_identity_mapping`, `oidc_configuration`)
- Webhook subscriptions and `X-JFrog-Event-Auth` / `use_secret_for_signing`:
  <https://github.com/jfrog/terraform-provider-artifactory> (`artifact_webhook`)
- Webhooks overview: <https://docs.jfrog.com/integrations/docs/webhooks>

## Documented versus still to confirm on a live instance

Documented (JFrog docs or JFrog's own provider code): the OIDC provider and token-exchange
endpoints; identity-mapping paths, methods and field names; `claims` as a JSON object;
`priority` ordering; 404 for a missing mapping or provider; `provider_type` `GitHub` and
`generic`; the `X-JFrog-Event-Auth` header and `use_secret_for_signing`.

Built from knowledge of the APIs, NOT confirmed against docs in this item (fixtures encode them):

- `repository_id` / `repository_owner_id` / `environment` accepted as mapping claims (ADR open
  issue; NG5 fixtures cannot prove it) and GHES/GHE.com tokens carrying them.
- The API string for the generic provider: sent as `Generic OpenID Connect` (the provider code
  maps `generic` to it); the docs page shows only `GitHub`.
- Projects API body (`project_key`, `display_name`, `description`), the 409 on duplicates, the
  key limits (2 to 32 lowercase alphanumerics assumed), and that a project must be empty before
  deletion.
- An unknown repository answering `400` or `404` on `GET /artifactory/api/repositories/{key}`;
  the "already exists" answer to `PUT` (assumed `400`); repository keys in a project must start
  with the project key.
- Webhook subscription body: `event_filter.domain` `docker`, `event_types` `["pushed"]`, camelCase
  `criteria` keys; the secret cannot be read back, so a changed secret alone is not detected.
- Access token creation body and response (`token_id`, `access_token`, `expires_in`), the
  `description` and `project_key` fields, the role scope string (the space in `Project Admin`
  inside a space-delimited scope is a known risk), a transient `username`, and the list response
  `{"tokens": [...]}` (no pagination handling).
- The identity-mapping list response shape (an array, or an object holding one).
- Which JFrog license/tier error an unlicensed OIDC call returns; the adapter treats any 4xx as
  non-retryable.
