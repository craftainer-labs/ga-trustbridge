# adapters/harbor/

The Harbor / Mirantis Secure Registry (MSR 4, Harbor-based) adapter (NG5b,
[ADR 0029](../../../../docs/adrs/0029-adapter-plugin-interface.md)). Entry point
`harbor = "trustbridge.adapters.harbor:HarborAdapter"` in the `trustbridge.adapters` group.

- `adapter.py` — `HarborAdapter` (all `PlatformAdapter` methods) and the naming helpers.
- `client.py` — the httpx client: HTTP basic auth, status-to-`AdapterError` mapping (429, 5xx and
  transport errors retry; other statuses do not), no response body in any error message.

Connection data per tenant: `base_url` (https, no query) in `AdapterConfig.connection`, `username`
(the admin or system-level robot account name), the matching secret as `privileged_credential`
(basic auth against `/api/v2.0`), optional `project_key` (the project robots and webhooks live in;
default: the tenant's `default` project), and one entry per webhook `secret_ref`. The adapter is
stateless; `HarborAdapter(config, *, transport=None, clock=None)` takes the last two for the
contract tests. `github_outputs` reads the constant per-instance `base_url` (user decision: that
counts as pure).

## What it provisions

| Interface method | Harbor call |
| --- | --- |
| `ensure_project` | `GET/POST /api/v2.0/projects[/{name}]`; name `tb` + 20 hex of `sha256(tenant hex \| spec key)`, created private; 409 on create is a race |
| `ensure_repository` | none: Harbor has no create-repository API (a repository appears on its first push), so it only returns the deterministic name `<project>/<key>` with `UNCHANGED` |
| `ensure_webhook` | `GET/POST/PUT /api/v2.0/projects/{p}/webhook/policies[/{id}]`, type `http`, `auth_header` = the secret |
| `delete` | `DELETE` on the project or the policy (policy found by name); a repository is never touched |
| `ensure_credential`, `rotate_begin` | `GET/POST /api/v2.0/robots`, `PATCH /api/v2.0/robots/{id}` (refresh the secret) |
| `rotate_finish`, `delete_credential` | `DELETE /api/v2.0/robots/{id}` (robot found by name) |
| `list_credentials` | `GET /api/v2.0/robots?q=name=~<tenant prefix>` |
| `health_check` | `GET /api/v2.0/users/current` (authenticated) |

## Grants: project-wide only (user decision "Harbor scope")

Harbor has no per-repository permission, so `CredentialScope.platform_repository_key` cannot
narrow a credential here. The adapter always issues one project-level robot account
(`level: project`, one `permissions` entry of `kind: project`, `namespace: <project>`) whose
`access` entries are on the `repository` resource:

| `Permission` | actions |
| --- | --- |
| `pull` | `pull` |
| `push` | `pull`, `push` |
| `admin` | `pull`, `push`, `delete` |

With `platform_repository_key` set the request is identical and no error is raised
(`test_ensure_credential_grants_the_whole_project_in_both_scopes`). The robot expires after
90 days (`duration: 90`).

Capabilities: `oidc` is false (Harbor has no GitHub OIDC trust), `rotation` is `single`, the
webhook is `STATIC_HEADER` in `Authorization`, trigger-only.

## Decisions

- **Rotation is `single` and in place.** Robot names are unique per project, so a second robot
  cannot be created under the same name. `rotate_begin` asks Harbor to refresh the robot's secret
  (`PATCH /robots/{id}`); the old secret stops working at once. `rotate_finish` deletes only a
  `previous` slot whose name differs from `current`.
- **Robot lookup.** The listing spans the whole instance, so a robot is selected by the suffix
  `<project>+<slot name>` of its full name (`<prefix><project>+<name>`, default prefix `robot$`).
- **Webhook.** Harbor sends `auth_header` verbatim as the `Authorization` request header, so the
  inbound check is a static-header comparison, never an HMAC; the payload is only a trigger.
  Default event `PUSH_ARTIFACT`.
- **Secret values.** `IssuedSecret.values` holds `username` (the robot's full name) and
  `password`; `github_outputs` writes variable `HARBOR_REGISTRY` and secrets `HARBOR_USERNAME` and
  `HARBOR_PASSWORD`.

## Sources (Harbor and MSR documentation, checked via context7 and the swagger file)

- Harbor REST API definition (`/api/v2.0`, basic auth; `Project`, `ProjectReq`, `Robot`,
  `RobotCreate`, `RobotCreated`, `RobotSec`, `RobotPermission`, `Access`, `WebhookPolicy`,
  `WebhookTargetObject`, the `q` query syntax `k=v`, `k=~v`, and 409 on a duplicate project):
  <https://raw.githubusercontent.com/goharbor/harbor/main/api/v2.0/swagger.yaml>
- `RobotPermission` has only `kind`, `namespace` and `access`; `Access` has only `resource`,
  `action` and `effect`. There is no repository field, which is the limitation below.
- Robot creation, permission validation and the `system`/`project` levels:
  <https://github.com/goharbor/harbor/blob/main/src/server/v2.0/handler/robot.go>,
  <https://github.com/goharbor/harbor/blob/main/src/controller/robot/model.go>
- Robot account prefix and the name format `<prefix><project_name>+<account_name>`:
  <https://goharbor.io/docs/main/administration/robot-accounts/>,
  <https://goharbor.io/docs/main/working-with-projects/project-configuration/create-robot-accounts/>
- Webhooks, event types and the default payload (`PUSH_ARTIFACT`):
  <https://goharbor.io/docs/main/working-with-projects/project-configuration/configure-webhooks/>
- MSR 4 is "based on Harbor" (no API differences are documented there): <https://docs.mirantis.com/msr/>

## Still to confirm on a live instance

No primary-documentation statement exists for these; the fixtures encode best-effort shapes:

- That `PATCH /robots/{id}` with `{"secret": ""}` makes Harbor generate a secret and return it in
  `RobotSec`. The swagger only says "Refresh the robot secret".
- That the robot list `q=name=~<text>` matches the full or stored name, and which prefix the
  listed `name` carries (`robot$` by default, configurable in system settings).
- That a robot name made of `tb-<hex>-<key>-g<n>` passes Harbor's robot name validation and
  length limit, and the answer for a duplicate name (409 assumed).
- That a duplicate webhook policy name on `POST` answers 409 (not documented), and that the
  policy listing accepts `q=name=<exact name>`. A changed secret alone is not detectable: Harbor
  does not return `auth_header` in the clear.
- That `auth_header` is sent as the `Authorization` header value, and the `payload_format` value
  `Default`.
- That `expires_at` in `RobotCreated` is an epoch in seconds (`-1` for never).
- That `DELETE /projects/{name}` answers 412 for a project that still holds repositories.
- Whether MSR 4 differs from upstream Harbor in any of these endpoints (its docs do not say).
- That `GET /projects/{name}` answers 404 (not 403) for an absent project to the admin account.
- Pagination: one page of 100 is read for robots and policies; `Link` headers are not followed.

## Known limitations

- **No per-repository grant.** Harbor robot permissions stop at project scope, so a credential
  issued for repository A can reach every repository in the same Harbor project. This is the
  user decision "Harbor scope"; `platform_repository_key` is accepted and ignored. Isolation
  between repositories needs separate Harbor projects, which this adapter does not create.
- A repository resource is never created or deleted by the adapter (Harbor creates it on push).
- Credentials target the adapter's configured project key; it must equal the key the projects
  were ensured under (nothing validates that they agree).
- A scope whose permission changes gets a new robot name (the scope key changes); the old robot is
  removed by `delete_credential` or the orphan sweep, not rewritten in place.
- An existing project, robot or policy is not compared on permissions or secret, only on the
  fields named above.
- `connection.base_url` is operator configuration and is not checked against an allow-list; no
  tenant or webhook input reaches it (as for the JFrog adapter).
