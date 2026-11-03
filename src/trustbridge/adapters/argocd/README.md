# adapters/argocd/

The Argo CD adapter (NG5d, [ADR 0029](../../../../docs/adrs/0029-adapter-plugin-interface.md)).
Entry point `argocd = "trustbridge.adapters.argocd:ArgoCDAdapter"` in the `trustbridge.adapters` group.

Argo CD is pull-based and has no CI credential. As ADR 0029 decides, the adapter drives Argo CD
through the **Kubernetes API of its cluster** (Secrets, ConfigMaps and `AppProject` custom
resources in the Argo CD namespace), not through Argo CD's own REST API. It never calls GitHub.

- `adapter.py` — `ArgoCDAdapter` (all `PlatformAdapter` methods) and `web_base`.
- `client.py` — the httpx client: bearer auth with the ServiceAccount token, status-to-`AdapterError`
  mapping (408, 425, 429, 5xx and transport errors retry; other statuses do not), no response body
  in any error message.

Connection data per tenant (`AdapterConfig.connection`, values are `SecretStr`):

| Key | Meaning |
| --- | --- |
| `base_url` | the Kubernetes API server (https, no query); operator-provisioned |
| `namespace` | optional; default `argocd` |
| `app_id`, `private_key` | the tenant's read-only GitHub App, written by the manifest flow (`github/manifest.py`, `_store_argocd`) |
| `installation_id` | written by `record_argocd_installation` once the org installed the App |
| one entry per webhook `secret_ref` | the bearer token Argo CD sends to trustbridge |

`privileged_credential` is the ServiceAccount token (RBAC below). The adapter uses exactly the keys
the manifest flow stores; it reads no key of trustbridge's own App and no signer (invariant A14).
A trust store for a private cluster CA is the process's (`SSL_CERT_FILE`); the adapter sets no
`verify` option. `AdapterConfig` holds only this tenant's data; the adapter is stateless
(`ArgoCDAdapter(config, *, transport=None)`; `transport` is for the contract tests).

## What it provisions

| Interface method | Kubernetes call |
| --- | --- |
| `ensure_project` | `GET/POST/PUT .../apis/argoproj.io/v1alpha1/namespaces/<ns>/appprojects[/{name}]`; name `tb-<tenant hex>-<digest>`; `spec.sourceRepos` and `spec.destinations` start empty |
| `ensure_repository` | `GET` + `PUT` of the AppProject: the repository's Git URL (`<web url>/<org>/<RepoSpec.key>`) is added to `spec.sourceRepos` |
| `ensure_webhook` | `GET` + merge `PATCH` of `argocd-notifications-secret` (key `<name>-token`) and `argocd-notifications-cm` (key `service.webhook.<name>`) |
| `delete` | `DELETE` of the project; `PUT` removing the URL from `sourceRepos`; merge `PATCH` with `null` removing the two webhook keys |
| `ensure_credential` | `GET/POST/PUT /api/v1/namespaces/<ns>/secrets/{slot name}`, one Secret per credential slot, returns `NoCiCredential` |
| `rotate_begin` | refused (`rotation` is `none`) |
| `rotate_finish`, `delete_credential` | `DELETE` of the slot's Secret |
| `list_credentials` | `GET .../secrets?labelSelector=app.kubernetes.io/managed-by=trustbridge,argocd.argoproj.io/secret-type`, filtered by the `tb-<tenant hex>-` name prefix |
| `github_outputs` | nothing (pull-based) |
| `health_check` | `GET .../appprojects?limit=1`, which proves URL, token, RBAC and the CRD |

The credential Secret carries `type: git`, `url`, `githubAppID`, `githubAppInstallationID`,
`githubAppPrivateKey` and, off github.com, `githubAppEnterpriseBaseUrl` (the host's API URL). While
`installation_id` is not recorded the call returns `NEEDS_MANUAL_ACTION` (install the App) with no
credential. The web URL of the host (`web_base`) is derived from `GitHubHost.api_url`:
`https://api.github.com` to `https://github.com`, `https://api.SUB.ghe.com` to
`https://SUB.ghe.com`, GHES `https://host/api/v3` to `https://host`.

## Grants: per repository or per organization

Argo CD has no per-credential permission levels; the grant is *which repositories the App key is
used for*. The `CredentialScope.platform_repository_key` decision maps to the two documented
Secret kinds:

- **Set** (the core names the `RepoSpec.key`): a `repository` Secret (label
  `argocd.argoproj.io/secret-type: repository`) whose `url` is exactly that repository. Argo CD uses
  these credentials for that repository only.
- **Unset**: a `repo-creds` Secret (credential template) whose `url` is the organization prefix
  `<web url>/<org>/`, so every repository of the tenant's org that no more specific credential
  covers. The tenant's App is per tenant and read-only (`contents: read`, `metadata`), so a leaked
  key can read only that org (ADR 0029 item 6). The claims-based narrowing JFrog does has no Argo CD
  equivalent; the AppProject's `sourceRepos` (set by `ensure_repository`) is what limits which
  repositories an Application may deploy from.
- A scope that gains or loses its repository key is rewritten in place under the same Secret
  name (the label and URL change); `permission` and `environment` do not matter to Argo CD.

## Decisions

- **Webhook authentication.** Argo CD notifications send a static header, so `capabilities().webhook`
  is `STATIC_HEADER`, header `Authorization` (`Bearer <token>`), trigger-only. The token is written
  to `argocd-notifications-secret` and referenced from the service as `$<key>`, as the docs do for
  `$github-token`; the per-tenant token never sits in the ConfigMap. trustbridge compares it in
  constant time on receipt (core side).
- **No OIDC, no rotation.** `oidc=False`, `ci_credential=False`, `rotation="none"`; the core never
  calls `rotate_begin`. The Secret holds the App's key, which GitHub (not trustbridge) rotates; a
  changed key in connection data is rewritten on the next `ensure_credential`.
- **Merge patches for shared objects.** `argocd-notifications-cm` and `-secret` are shared with the
  operator, so the adapter only touches its own keys with `application/merge-patch+json` and never
  replaces the object. An AppProject is replaced with `PUT` carrying the `resourceVersion` it read;
  a `409` is a retryable error and the retry re-reads.
- **Resource names** use `resource_name` and `credential_name`; every name embeds the whole tenant
  id (A4). The adapter labels its Secrets `app.kubernetes.io/managed-by: trustbridge`.

## ServiceAccount RBAC the operator provisions

In the Argo CD namespace only: `get`, `list`, `create`, `update` on `secrets`; `delete` on `secrets`;
`get`, `patch` on `configmaps` and `secrets` named `argocd-notifications-cm` /
`argocd-notifications-secret`; `get`, `list`, `create`, `update`, `delete` on
`argoproj.io` `appprojects`. Nothing cluster-wide. (A `resourceNames` rule cannot restrict `create`
or `list`, so `secrets` is namespace-wide for those verbs.)

## Sources

- Repository Secrets, GitHub App fields (`githubAppID`, `githubAppInstallationID`,
  `githubAppPrivateKey`, `githubAppEnterpriseBaseUrl`), `repo-creds`, `project` field, AppProject:
  <https://argo-cd.readthedocs.io/en/stable/operator-manual/declarative-setup/>
- `repo-creds` example with the GitHub App fields:
  <https://argo-cd.readthedocs.io/en/stable/operator-manual/argocd-repo-creds-yaml/>
- AppProject fields (`description`, `sourceRepos`, `destinations`):
  <https://argo-cd.readthedocs.io/en/stable/operator-manual/project-specification/>
- Notifications webhook service (`service.webhook.<name>`, `url`, `headers`, `$key` references):
  <https://argo-cd.readthedocs.io/en/stable/operator-manual/notifications/services/webhook/>,
  <https://argo-cd.readthedocs.io/en/stable/operator-manual/notifications/services/overview/>
- Kubernetes API conventions (merge patch content type, `labelSelector`, `resourceVersion` and `409`):
  <https://kubernetes.io/docs/reference/using-api/api-concepts/>
- `argocd repocreds add` (trailing slash on the URL prefix, optional installation id):
  <https://argo-cd.readthedocs.io/en/stable/user-guide/commands/argocd_repocreds_add/>

## Still to confirm on a live instance

The fixtures encode these as best-effort shapes; check them against a real Argo CD first:

- That `repo-creds` URL matching honours the trailing slash in `<web url>/<org>/` (the declarative
  examples show no slash, the CLI example shows one). The slash is meant to stop `acme` from
  matching `acme-other`.
- That an Argo CD Secret may carry a `repository` entry with GitHub App fields for a URL the
  tenant's `repo-creds` template also covers (the more specific entry is expected to win).
- GHE.com: that `githubAppEnterpriseBaseUrl` set to `https://api.SUB.ghe.com` (the host's
  `api_url`) is what Argo CD expects. The docs show only the GHES form `https://host/api/v3`.
- The derived web URL of a GHE.com host (`https://SUB.ghe.com`) and that `RepoSpec.key` is the
  GitHub repository name.
- That `PUT` of a Secret without `metadata.resourceVersion` is accepted (an unconditional update,
  expected for core types), and that `stringData` is accepted in a merge `PATCH` of a Secret.
- That `argocd-notifications-cm` and `argocd-notifications-secret` exist in the namespace (Argo CD
  3.x ships them with its install manifests) and that Argo CD reloads a changed `service.webhook.*`
  key without a restart.
- That the `Authorization: Bearer $<key>` header form works for the webhook service (the docs show
  `token $github-token`) and that a key containing `-` is a valid `$` reference.
- An AppProject with empty `destinations` and `sourceRepos` is accepted by the CRD.
- The ServiceAccount RBAC above is sufficient, including `list` with a `labelSelector`.

## Known limitations

- Destinations: a new AppProject allows no deployment destination, because the interface carries
  none. An operator (or a later item) must add `spec.destinations` by hand.
- The notifications trigger, template and the per-Application subscription annotations are not
  provisioned; `WebhookTarget.events` is not used. The adapter writes only the webhook service and
  its token, so nothing is sent until an operator adds a trigger and template.
- `ensure_repository` is a read-modify-write of the AppProject; two workers adding different URLs at
  once resolve through the `409` retry. Removing a project does not remove Secrets written by
  `ensure_credential`; the credential delete and the orphan sweep do.
- `list_credentials` does not follow Kubernetes list `continue` tokens (no `limit` is sent, so a
  large namespace is returned whole).
- A changed webhook `secret_ref` value is rewritten on the next call, but a webhook deleted by hand
  from the ConfigMap while its token key remains is recreated as `CREATED`, not `UPDATED`.
- `connection.base_url` is operator configuration and is not checked against an allow-list. No
  tenant or webhook input reaches it: the only writer of connection data, `ConnectionData.put`
  (`worker/ports.py`), has one caller (`github/manifest.py`) that writes the fixed keys `app_id`,
  `private_key` and `installation_id` to the `argocd` entry, so `base_url`, `namespace` and the
  ServiceAccount token are provisioned in the secret store and read back by `config_for`. A future
  admin-API writer of connection data needs the same review (SSRF with the ServiceAccount token).
