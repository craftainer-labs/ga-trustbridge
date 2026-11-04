# adapters/ghcr/

The GHCR (GitHub Container Registry) adapter (NG5e, [ADR 0029](../../../../docs/adrs/0029-adapter-plugin-interface.md)).
Entry point `ghcr = "trustbridge.adapters.ghcr:GhcrAdapter"` in the `trustbridge.adapters` group.

- `adapter.py` - `GhcrAdapter` (all `PlatformAdapter` methods) and the naming helpers.

GHCR lives inside GitHub, so the adapter has **no connection data, no privileged credential and
makes no network call**. It cannot call GitHub (adapters never do, and it must not import the
GitHub core), and the one API that could read package state needs a token trustbridge does not
hold (see below). `GhcrAdapter(config)` takes only the `AdapterConfig`.

## What it does

| Interface method | Behaviour |
| --- | --- |
| `capabilities` | `oidc=False`, `oidc_hosts` empty, `ci_credential=False`, `rotation="none"`, `webhook=None`, all three host kinds, no privileged credential |
| `ensure_project` | records the namespace (the tenant's org, lowercased) and the host's `registry_host`; `CREATED` the first time (recorded, nothing exists to create), then `UNCHANGED`, or `UPDATED` if the registry host changed |
| `ensure_repository` | validates and records a container package name (`<registry_host>/<org>/<name>`, one lowercase segment). With `RepoSpec.repository_id` set it returns `NEEDS_MANUAL_ACTION` naming the UI step; without it, only the record |
| `ensure_webhook` | non-retryable `AdapterError`: GHCR has no webhooks of its own (`capabilities().webhook` is `None`) |
| `delete` | `UNCHANGED`, state `None`: the record is forgotten, a package is **never** deleted |
| `ensure_credential`, `rotate_begin` | `NoCiCredential`; CI authenticates with the job's `GITHUB_TOKEN` |
| `rotate_finish`, `delete_credential` | `UNCHANGED` (nothing was created) |
| `list_credentials` | `[]` |
| `github_outputs` | `[]` (a job's `GITHUB_TOKEN` needs no secret or variable) |
| `health_check` | always `ok` |

The adapter never adopts, changes or deletes a package, a visibility or an access setting: it
creates none, so it has nothing of its own to remove.

## Credential: none beyond `GITHUB_TOKEN`

Workflows push with `GITHUB_TOKEN` and `permissions: packages: write` (plus `contents: read`).
A package published by a workflow inherits the visibility and permissions of the workflow's
repository, and that repository is granted `admin` on it; an existing package needs the
repository added under Manage Actions access. The result is `NoCiCredential`, as the ADR's
capability matrix says.

## Grants: per package or namespace-wide

`CredentialScope.platform_repository_key` (the `RepoSpec.key` the credential is for):

- **Set:** the grant is that one package. GitHub grants package access per package (Manage
  Actions access, role Read, Write or Admin), so this maps without a gap. `NoCiCredential.reason`
  names the package; the access itself is the manual step reported by `ensure_repository`.
- **Unset:** the grant is namespace-wide: workflows of any repository the org lets use
  `packages: write`. GHCR has no project object to narrow, so nothing narrows it below the org
  except the claims the core already applies and each package's own access list.

## Why repository access is `NEEDS_MANUAL_ACTION`

The Packages REST API lists, gets, deletes and restores packages and versions (27 endpoints, none
sets repository access or visibility; the `visibility` parameter only filters lists). The access
steps ("Manage Actions access" > "Add repository", the role, "Inherit access from repository")
are documented as web-UI steps. So the adapter reports the step through `Change.manual_action`.

Two consequences, both for a person to know:

- The adapter cannot see whether the step was done: the Packages endpoints take a personal access
  token (classic) or OAuth token with `read:packages`, not a GitHub App installation token, and
  they expose no access list anyway. `ensure_repository` therefore reports
  `NEEDS_MANUAL_ACTION` on every run while `repository_id` is set.
- The step is not needed when the first push comes from the repository's own workflow with
  `GITHUB_TOKEN` (access is inherited), or when the image carries
  `org.opencontainers.image.source`. It matters for a package first pushed with a personal token.

## Contract tests and fixtures

`tests/trustbridge/adapters/ghcr/test_ghcr_contract.py` covers every I2 operation and both grant
scopes. There are no recorded API fixtures: the adapter makes no API exchange, so there is nothing
to replay.

## Sources

- Container registry (`ghcr.io`, `org.opencontainers.image.source` linking and inherited access,
  `GITHUB_TOKEN`, default private visibility):
  <https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry>
- Package access control and visibility (Manage Actions access, Inherit access from repository,
  roles; the web-UI steps): <https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility>
- Publishing with Actions (`packages: write`, `contents: read`, inheritance and automatic `admin`
  for the publishing repository, Manage Actions access for existing packages):
  <https://docs.github.com/en/packages/managing-github-packages-using-github-actions-workflows/publishing-and-installing-a-package-with-github-actions>
- Packages REST API (endpoint list, token scopes, no access or visibility endpoint):
  <https://docs.github.com/en/rest/packages/packages>

## Still to confirm on a live instance

- That GHES (`containers.HOST`) and GHE.com (`containers.SUB.ghe.com`) give the same access model
  and UI steps; the container-registry page names only `ghcr.io`.
- That the org's "inheritance disabled" setting and any enterprise package policy do not block a
  workflow-published package from inheriting repository access.
- Whether the `GITHUB_TOKEN` can push to a package first created with a personal token in the
  same namespace once the repository is added under Manage Actions access (the container page
  says it cannot push before the package is connected).
- That the Packages REST API is still without an access endpoint on the target GitHub version; if
  one appears, `ensure_repository` can read and set it and the manual step goes away.
- Whether a lowercase-only package name is enforced on every host (the docs do not say; the
  adapter requires it).

## Known limitations

- Nested package names (`name/sub`) are rejected; only a single segment is accepted.
- `NEEDS_MANUAL_ACTION` repeats on every reconcile while `repository_id` is set, because completion
  cannot be read back.
