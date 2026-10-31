# 0029. Adapter plugin interface for platform adapters (entry-point group `trustbridge.adapters`)

## Status

Proposed. For the user's API gate (G-api): this ADR defines a public API
that third parties may implement. See "Decisions for the user (G-api)".
Part of the set [0028](0028-trustbridge-architecture-and-instance-layout.md)
(architecture), this one, and
[0030](0030-key-signer-and-github-hosts.md) (signer and hosts).

## Context

trustbridge provisions things on artifact and deployment platforms and
hands CI the credentials to use them. The family plan (interface I2)
sketched a `PlatformAdapter` Protocol. Research (NR5, 2026-10-31) changed
the inputs:

- **Harbor** (and Mirantis MSR 4, which is Harbor 2.11-2.13) has no OIDC
  federation in any upstream release. A robot secret refresh is a hard
  cut, so overlap needs two robot accounts.
- **JFrog** OIDC needs Enterprise X or Enterprise+. GHES and GHE.com use
  `provider_type: generic` with the host's issuer. Identity mappings are
  scoped on the `repository` / `repository_owner` claims, never on a
  hand-built `sub` (GitHub's immutable-ID `sub` for repositories created
  after 2026-07-15).
- **Docker Hub**: an org access token (OAT) cannot mint OATs; Team allows
  10 OATs per org; OIDC connections are UI-only and github.com-only.
- **Argo CD** is pull-based: there is no CI credential. It is driven
  through the Kubernetes API of its cluster; its notifications reach the
  App as inbound webhooks with a per-tenant bearer header.
- **GHCR** needs nothing provisioned; CI uses `GITHUB_TOKEN`. The registry
  host derives from the GitHub host.

Constraints: operations run at least once (ADR 0028), so they must be
idempotent; credentials should be OIDC trust first (EB-9); adapters must
not call GitHub (the core alone writes secrets, variables and
environments, using one signer-backed credential path).

## Decision

### Discovery

Adapters are Python distributions that declare an entry point:

```toml
[project.entry-points."trustbridge.adapters"]
jfrog = "trustbridge.adapters.jfrog:JFrogAdapter"
```

The core loads `importlib.metadata.entry_points(group="trustbridge.adapters")`,
instantiates each enabled adapter with an `AdapterConfig` (per-tenant
connection data resolved from the secret store), and checks
`API_VERSION` against the supported range. The five built-in adapters use
the same mechanism. An adapter not listed in the deployment's
`ADAPTERS_ENABLED` setting is never loaded.

### Types

```python
from __future__ import annotations
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import ClassVar, Literal, Protocol, runtime_checkable

from pydantic import SecretStr

API_VERSION = 1   # int; bumped on a breaking change to this module

# ---- hosts and scope ---------------------------------------------------
@dataclass(frozen=True, slots=True)
class GitHubHost:
    api_url: str          # https://api.github.com | https://HOST/api/v3 | https://api.SUB.ghe.com
    oidc_issuer: str      # per host, configured (ADR 0030)
    registry_host: str    # ghcr.io | containers.HOST | SUB.ghe.com container registry host

@dataclass(frozen=True, slots=True)
class Tenant:
    id: str               # surrogate tenant id (UUID) mapping to (host, installation_id); installation ids are unique only per host
    github_host: GitHubHost
    org: str              # owning org / user login, informational only

@dataclass(frozen=True, slots=True)
class CredentialScope:
    tenant: Tenant
    github_host: GitHubHost            # same as tenant.github_host; explicit for adapters
    repository_id: int | None          # durable numeric id; None = org-wide
    repository_owner_id: int
    environment: str | None
    permission: Literal["pull", "push", "admin"]

# ---- capabilities ------------------------------------------------------
class WebhookAuth(StrEnum):
    HMAC = "hmac"                  # platform signs the body with a shared secret
    STATIC_HEADER = "static_header"  # fixed header value (e.g. Argo CD notifications bearer)
    NONE = "none"                  # no authentication possible

@dataclass(frozen=True, slots=True)
class WebhookCapability:
    auth: WebhookAuth
    header: str | None = None
    # NONE and STATIC_HEADER payloads are never trusted: the core only uses
    # them as a trigger to re-read state through the platform's API.
    trigger_only: bool = True

@dataclass(frozen=True, slots=True)
class Capabilities:
    oidc: bool                         # can establish OIDC trust for CI
    oidc_requires_manual_step: bool    # trust cannot be created via API (Docker Hub)
    ci_credential: bool                # False: pull-based, CI needs none (Argo CD)
    rotation: Literal["none", "single", "two_phase"]
    webhook: WebhookCapability | None
    github_hosts: frozenset[Literal["github.com", "ghes", "ghe.com"]]
    needs_privileged_credential: bool  # adapter itself holds a long-lived admin credential

# ---- results -----------------------------------------------------------
class ChangeState(StrEnum):
    UNCHANGED = "unchanged"
    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"
    NEEDS_MANUAL_ACTION = "needs_manual_action"   # e.g. Docker Hub OIDC connection

@dataclass(frozen=True, slots=True)
class Change:
    state: ChangeState
    ref: Mapping[str, str]            # opaque platform identifiers for AdapterState
    manual_action: str | None = None  # human instructions when NEEDS_MANUAL_ACTION
    detail: str | None = None         # audit-safe message; never contains secrets

@dataclass(frozen=True, slots=True)
class OidcTrust:
    issuer: str                       # the host's oidc_issuer
    audience: str | None
    subject_claims: Mapping[str, str] # claim -> value; repository / repository_owner, not `sub`
    ref: Mapping[str, str]

@dataclass(frozen=True, slots=True)
class IssuedSecret:
    ref: Mapping[str, str]            # platform ids (e.g. robot id), safe to store
    values: Mapping[str, SecretStr]   # secret material, never logged; held only in memory and the secret store
    expires_at: datetime | None
    retire_after: datetime | None = None   # set on a rotation: when the previous one may be retired

@dataclass(frozen=True, slots=True)
class NoCiCredential:
    reason: str                       # e.g. "pull-based GitOps"

@dataclass(frozen=True, slots=True)
class GitHubOutput:
    kind: Literal["secret", "variable", "environment"]
    name: str
    value: SecretStr | str
    repository_id: int | None
    environment: str | None

@dataclass(frozen=True, slots=True)
class Health:
    ok: bool
    detail: str | None = None

# ---- the protocol ------------------------------------------------------
@runtime_checkable
class PlatformAdapter(Protocol):
    name: ClassVar[str]
    api_version: ClassVar[int]

    def capabilities(self) -> Capabilities: ...

    async def ensure_project(self, t: Tenant, s: ProjectSpec) -> Change: ...
    async def ensure_repository(self, p: ProjectRef, s: RepoSpec) -> Change: ...
    async def ensure_webhook(self, t: WebhookTarget) -> Change: ...

    async def ensure_credential(
        self, s: CredentialScope
    ) -> OidcTrust | IssuedSecret | NoCiCredential | Change: ...
    async def rotate_begin(self, current: Mapping[str, str]) -> IssuedSecret: ...   # current = ref of the live credential
    async def rotate_finish(self, old: Mapping[str, str]) -> Change: ...            # old = ref only; values are never needed

    def github_outputs(self, state: AdapterState) -> Sequence[GitHubOutput]: ...
    async def delete(self, state: AdapterState) -> Change: ...
    async def health_check(self) -> Health: ...
```

`ProjectSpec`, `ProjectRef`, `RepoSpec`, `WebhookTarget` and
`AdapterState` are frozen dataclasses with the same conventions (opaque
`ref` mappings, no secrets in them); the final fields are fixed in the
NG4 interface freeze and listed in the contract tests. `SecretStr` is
pydantic's.

### Semantics

- **Reconcile, not command.** Every `ensure_*` reads the platform's
  current state and converges on the spec; calling it twice yields
  `UNCHANGED` the second time. The exception is secret material, which
  cannot be read back: before issuing, `ensure_credential` receives the
  stored `ref` (via `CredentialScope`/`AdapterState`) and looks the
  credential up by it. If it exists and the core has recorded its
  values as delivered, the result is `Change(UNCHANGED)`; if it exists
  but delivery is unrecorded (a crash between issue and the GitHub
  write), the adapter deletes it and issues a fresh one, so at most one
  orphan-candidate exists per scope. Credentials are named
  deterministically (tenant, scope, generation) so orphans can be found
  by a periodic sweep that deletes credentials with no matching ref. Identity is by durable platform ids and
  repository numeric ids, never by name or handle. `delete` is also
  idempotent (absent = `UNCHANGED`).
- **OIDC first.** `ensure_credential` returns `OidcTrust` when
  `capabilities().oidc` is true and the host allows it. It falls back to
  `IssuedSecret`. `NoCiCredential` means CI needs nothing (Argo CD). A
  `Change` with `NEEDS_MANUAL_ACTION` means the adapter cannot finish via
  API (Docker Hub's OIDC connection): the core surfaces
  `manual_action` in the UI and as a check-run note, and re-runs the
  operation until the platform reports the trust.
- **Two-phase rotation.** `rotate_begin(current)` takes only the `ref`
  of the live credential (the core stores refs, not values) and creates the new credential
  while the old stays valid and returns it with `retire_after` set. The
  core writes the new values to GitHub, and calls `rotate_finish(old_ref)`
  only after `retire_after` (grace period for in-flight CI). For
  `rotation="single"` platforms a hard cut is unavoidable and the
  adapter declares it. Harbor overlaps with two robots; Docker Hub with
  two OATs (mind the 10-OAT limit).
- **Adapters never call GitHub.** They return `GitHubOutput`s; the core
  alone writes encrypted secrets, variables and environments.
- **Secrets.** `values` are `SecretStr`, never logged, never stored by
  the core except through the secret store (the core persists only
  `ref`); `Change.detail` and
  exceptions must not contain them.
- **Webhooks.** `capabilities().webhook` describes how inbound events are
  authenticated. For `NONE` and `STATIC_HEADER` the payload is never
  acted on; it only triggers a re-read via the platform API. Static
  headers are compared in constant time.
- **Errors.** Adapters raise `AdapterError(retryable: bool, message)`;
  retryable errors go back to the stream's retry path.
- **Versioning.** `API_VERSION` is an integer; a breaking change bumps
  it and the core refuses adapters outside its supported range. Additive
  fields to dataclasses are non-breaking (new fields get defaults).

### Capability matrix for the MVP adapters

| Adapter | Provisions | `oidc` | `ci_credential` | `rotation` | Webhook auth | GitHub hosts | Privileged credential held |
| --- | --- | --- | --- | --- | --- | --- | --- |
| JFrog Artifactory | project, repos, OIDC identity mapping (`/access/api/v1/oidc`) | yes (Enterprise X/Enterprise+; GHES/GHE.com via `generic` provider with the host's issuer; mappings on `repository`/`repository_owner`) | yes | none (trust, no secret) | `HMAC` or `STATIC_HEADER` depending on whether a webhook secret is set (JFrog is reported to sign with HMAC-SHA256 in `X-JFrog-Event-Auth`; not confirmed against current JFrog docs, NG5 fixtures settle it); trigger-only | all | admin access token |
| Harbor / MSR 4 (2.11-2.13) | project, robot account, webhook policy | no (no upstream OIDC federation) | yes, robot token | `two_phase` (two robots) | `STATIC_HEADER` (Harbor webhook auth header); trigger-only | all | system or project admin robot |
| Docker Hub | repository, org access token | partial: OIDC connection is UI-only and github.com-only, so `NEEDS_MANUAL_ACTION` | yes, OAT (fallback) | `two_phase` (watch the 10-OAT Team limit) | `NONE` (Docker Hub webhooks are unauthenticated); trigger-only | all (OAT path); OIDC only github.com | **org-owner PAT** (see G-api) |
| Argo CD | repository Secret using GitHub App credentials, AppProject, Application/ApplicationSet | n/a | **no** (`NoCiCredential`, pull-based) | none | `STATIC_HEADER` bearer, per tenant, constant-time compare; trigger-only | all | Kubernetes ServiceAccount with narrow RBAC; **raw App private key** held in the repo Secret (see G-api) |
| GHCR | nothing | no (credential is `GITHUB_TOKEN`, no trust to establish) | yes, `GITHUB_TOKEN` | none | n/a (GitHub events) | all; registry host derived from `GitHubHost.registry_host` | none |

Nexus stays out of the MVP.

## Decisions for the user (G-api)

1. **The interface itself** (entry-point group, protocol, dataclasses,
   `ChangeState`, two-phase rotation, `API_VERSION`) as a public API.
   Lean: approve as written; additive changes later do not need a gate,
   breaking ones bump `API_VERSION`.
2. **Argo CD repository credential needs a raw GitHub App private key.**
   Argo CD's repo-creds with GitHub App auth stores the App's private
   key in a cluster Secret that Argo CD itself reads.
   - A: reuse trustbridge's own App key. Feasible only with the dev
     `file` signer: with a non-exportable signer (Vault Transit, KMS,
     import-and-delete, ADR 0030) there is no key to place in Argo CD's
     Secret, and it would violate EB-9. Even then the Secret holds the
     key that can mint tokens for every tenant's installation, so
     compromise of Argo CD's namespace is compromise of trustbridge.
     Not a real option for production; B or C remain.
   - B: a **separate, read-only GitHub App** (contents: read, metadata)
     registered per tenant or per host, whose key is the only one given
     to Argo CD. Cost: another App to register (the manifest flow
     automates it) and to rotate.
   - C: skip repo-creds with an App key; use a deploy key or a
     per-repo fine-grained token. Cost: long-lived credential per
     repository, against EB-9.
   - Lean: B (NR5's suggestion). Least privilege, blast radius is
     read-only on that tenant.
3. **Docker Hub needs a long-lived org-owner PAT.** An OAT cannot mint
   OATs, so the adapter holds a PAT of an org owner to create and rotate
   OATs.
   - A: accept the PAT, stored via the signer/secret store, scoped to a
     dedicated service user that is an org owner, rotated by the
     customer; the adapter declares `needs_privileged_credential` and
     the UI shows it. Cost: a long-lived credential with owner rights
     (an exception to EB-9, because no federation exists).
   - B: do not mint OATs: the customer creates the OAT and the OIDC
     connection by hand (`NEEDS_MANUAL_ACTION`), trustbridge only
     stores/rotates nothing. Cost: manual work, no rotation.
   - C: drop Docker Hub from the MVP.
   - Lean: A, opt-in per tenant and off by default, with B as the
     default path; A is enabled only when the tenant supplies the PAT.
4. **Secret-issuance retry policy** (proposal): lookup by stored `ref`,
   delete-and-reissue when delivery is unrecorded, periodic orphan sweep
   on deterministic names. Alternative: leave orphans to the customer.
   Lean: as proposed (the Docker Hub 10-OAT cap makes orphans costly).
5. **Tenant identity** (proposal, shared with ADR 0028): surrogate
   tenant id mapping to `(host, installation_id)`.
6. **JFrog webhook authentication** is unconfirmed against current JFrog
   docs (HMAC vs static header); settled by NG5 fixtures, no decision
   needed unless it changes the capability model.
7. The **MVP set** is JFrog, Harbor/MSR, Docker Hub, Argo CD, GHCR
   (Nexus out). Lean: approve.

## Consequences

- One shape covers OIDC-capable, secret-based, manual and pull-based
  platforms, so the core has no per-vendor branches.
- Idempotency and `retire_after` make at-least-once delivery and
  rotation grace periods safe for everything except secret issuance,
  which relies on the ref lookup and orphan rules above; the cost is
  every adapter doing a read before every write.
- Public API: third-party adapters are possible, and the interface can
  only grow additively. `API_VERSION` is the escape hatch.
- Contract tests per adapter run against recorded API fixtures (NG5);
  Harbor fixtures cover 2.13.
- Adapters that hold privileged credentials are visibly flagged
  (`needs_privileged_credential`), feeding the audit log.

## Sources

- GitHub OIDC reference (claims, issuer, immutable `sub`):
  https://docs.github.com/en/actions/reference/security/oidc
- Python entry points:
  https://packaging.python.org/en/latest/specifications/entry-points/
- JFrog OIDC integration:
  https://docs.jfrog.com/administration/docs/openid-connect-integration
- Harbor API (robot accounts): https://goharbor.io/docs/main/working-with-projects/project-configuration/create-robot-accounts/
- Docker Hub access tokens (OATs):
  https://docs.docker.com/security/access-tokens/organization-access-tokens/
- Argo CD declarative setup (repository credentials, GitHub App):
  https://argo-cd.readthedocs.io/en/stable/operator-manual/declarative-setup/
- Argo CD notifications webhook service:
  https://argo-cd.readthedocs.io/en/stable/operator-manual/notifications/services/webhook/
- GHCR / `GITHUB_TOKEN` packages:
  https://docs.github.com/en/packages/managing-github-packages-using-github-actions-workflows/publishing-and-installing-a-package-with-github-actions
- Detailed NR5 findings: family plan Decision log, 2026-10-31 (NR5).
