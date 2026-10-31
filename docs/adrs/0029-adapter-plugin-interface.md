# 0029. Adapter plugin interface for platform adapters (entry-point group `trustbridge.adapters`)

## Status

Proposed. For the user's API gate (G-api): this ADR defines a public API
that third parties may implement. See "Decisions for the user (G-api)".
Part of the set [0028](0028-trustbridge-architecture-and-instance-layout.md)
(architecture; it owns the tables, leases and process roles this ADR
relies on), this one, and
[0030](0030-key-signer-and-github-hosts.md) (signer and hosts; it owns
`GitHubHostConfig`, from which `GitHubHost` below is projected).

## Context

trustbridge provisions things on artifact and deployment platforms and
hands CI the credentials to use them. The family plan (interface I2)
sketched a `PlatformAdapter` Protocol. Research (NR5, 2026-10-31) changed
the inputs:

- **Harbor** (and Mirantis MSR 4, which is Harbor 2.11-2.13) has no OIDC
  federation in any upstream release. A robot secret refresh is a hard
  cut, so overlap needs two robot accounts.
- **JFrog** OIDC needs Enterprise X or Enterprise+. GHES and GHE.com use
  `provider_type: generic` with the host's issuer. Identity mappings must
  not match a hand-built `sub` (GitHub's immutable-ID `sub` for
  repositories created after 2026-07-15). NR5 named the `repository` /
  `repository_owner` claims; those are mutable names, so this ADR matches
  the durable `repository_id` / `repository_owner_id` claims instead (see
  "OIDC trust claims").
- **Docker Hub**: an org access token (OAT) cannot mint OATs; Team allows
  10 OATs per org; OIDC connections are UI-only and github.com-only.
- **Argo CD** is pull-based: there is no CI credential. It is driven
  through the Kubernetes API of its cluster; its notifications reach the
  App as inbound webhooks with a per-tenant bearer header.
- **GHCR** needs nothing provisioned; CI uses `GITHUB_TOKEN`. The registry
  host derives from the GitHub host.

Constraints: operations run at least once (ADR 0028), several workers run
in parallel, and a process can die between any two statements. So every
call is idempotent, calls on one scope are serialised, and a secret-bearing
call is written ahead (see "Persistence model"). Credentials are OIDC trust
first (EB-9). Adapters must not call GitHub (the core alone writes
secrets, variables and environments, using one signer-backed credential
path).

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

HostKind = Literal["github.com", "ghes", "ghe.com"]

# ---- hosts and scope ---------------------------------------------------
@dataclass(frozen=True, slots=True)
class GitHubHost:
    # Built from GitHubHostConfig (ADR 0030) by GitHubHostConfig.to_host().
    name: str             # the configured host key, e.g. "github.com", "ghes-prod"
    kind: HostKind        # the core checks it against Capabilities before any call
    api_url: str          # https://api.github.com | https://HOST/api/v3 | https://api.SUB.ghe.com
    oidc_issuer: str      # per host, configured (ADR 0030)
    oidc_id_claims: bool  # tokens from this host carry repository_id / repository_owner_id (ADR 0030)
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
    key: str                           # scope_key, computed by the core: hex sha256 prefix of (repository_id|"org", environment|"", permission)

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
    oidc: bool                         # can establish OIDC trust for CI on at least one host kind
    oidc_hosts: frozenset[HostKind]    # host kinds where trust can be established; empty when oidc is False, subset of github_hosts
    oidc_requires_manual_step: bool    # trust cannot be created via API (Docker Hub)
    ci_credential: bool                # False: trustbridge provisions no CI credential (Argo CD pull-based; GHCR uses GITHUB_TOKEN)
    rotation: Literal["none", "single", "two_phase"]
    webhook: WebhookCapability | None
    github_hosts: frozenset[HostKind]  # host kinds the adapter works with; the core matches GitHubHost.kind against it
    needs_privileged_credential: bool  # adapter itself holds a long-lived admin credential
    credential_limit: int | None = None   # platform cap on credentials per (tenant, adapter), e.g. 10 for Docker Hub OATs

# ---- persisted state (the core stores it; adapters read it and return the new one) ----
@dataclass(frozen=True, slots=True)
class ResourceState:
    """Non-secret resource (project, repository, webhook). No secrets, ever."""
    name: str                         # deterministic platform-side name, chosen by the core
    ref: Mapping[str, str]            # platform ids; empty until the platform confirmed creation

@dataclass(frozen=True, slots=True)
class CredentialSlot:
    generation: int                   # 1-based; part of the deterministic name
    name: str                         # tb-<tenant uuid[:8]>-<scope.key[:10]>-g<generation>; adapters may shorten it deterministically
    ref: Mapping[str, str]            # platform ids; empty until the platform confirmed creation
    delivered: bool                   # the core wrote the values to GitHub (True for OIDC trust: nothing secret to deliver)
    retire_after: datetime | None = None   # set by the core on a slot being replaced; adapters ignore it

@dataclass(frozen=True, slots=True)
class CredentialState:
    current: CredentialSlot | None    # what CI uses, or is about to use (delivered=False). None only for adapters with ci_credential=False
    previous: CredentialSlot | None = None   # rotation: still valid until retire_after; rotate_finish deletes it

@dataclass(frozen=True, slots=True)
class ListedCredential:
    name: str
    ref: Mapping[str, str]

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
    manual_action: str | None = None  # human instructions when NEEDS_MANUAL_ACTION
    detail: str | None = None         # audit-safe message; never contains secrets

@dataclass(frozen=True, slots=True)
class Applied:
    change: Change
    state: ResourceState | None       # the complete new state; None after a delete

@dataclass(frozen=True, slots=True)
class OidcTrust:
    issuer: str                       # the host's oidc_issuer
    audience: str | None
    claims: Mapping[str, str]         # claim -> value the platform must require: repository_id, repository_owner_id (+ environment); never `sub`, never the name claims

@dataclass(frozen=True, slots=True)
class IssuedSecret:
    values: Mapping[str, SecretStr]   # secret material, never logged; held in memory and, until delivered, in the operation value store
    expires_at: datetime | None

@dataclass(frozen=True, slots=True)
class NoCiCredential:
    reason: str                       # e.g. "pull-based GitOps", "CI uses GITHUB_TOKEN"

@dataclass(frozen=True, slots=True)
class CredentialOutcome:
    change: Change
    state: CredentialState            # the complete new state; the core persists it as returned
    credential: OidcTrust | IssuedSecret | NoCiCredential | None
    # credential is None when there is nothing new to deliver: UNCHANGED, NEEDS_MANUAL_ACTION,
    # rotate_finish, delete_credential.

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

    # Non-secret resources. prior is None the first time; with an empty ref, look up by prior.name.
    async def ensure_project(self, t: Tenant, s: ProjectSpec, prior: ResourceState | None) -> Applied: ...
    async def ensure_repository(self, p: ProjectRef, s: RepoSpec, prior: ResourceState | None) -> Applied: ...
    async def ensure_webhook(self, t: WebhookTarget, prior: ResourceState | None) -> Applied: ...
    async def delete(self, prior: ResourceState) -> Applied: ...

    # Credentials. prior is the write-ahead state the core committed before this call.
    async def ensure_credential(self, s: CredentialScope, prior: CredentialState) -> CredentialOutcome: ...
    async def rotate_begin(self, s: CredentialScope, prior: CredentialState) -> CredentialOutcome: ...
    async def rotate_finish(self, s: CredentialScope, prior: CredentialState) -> CredentialOutcome: ...
    async def delete_credential(self, s: CredentialScope, prior: CredentialState) -> CredentialOutcome: ...
    async def list_credentials(self, t: Tenant) -> Sequence[ListedCredential]: ...   # trustbridge-named ones only (prefix tb-<tenant8>-), for the orphan sweep

    def github_outputs(
        self, s: CredentialScope, credential: OidcTrust | IssuedSecret | NoCiCredential
    ) -> Sequence[GitHubOutput]: ...
    async def health_check(self) -> Health: ...
```

`ProjectSpec`, `RepoSpec` and `WebhookTarget` are frozen dataclasses with
the same conventions (no secrets); `ProjectRef` is `(tenant: Tenant,
project: ResourceState)`. The final fields of those four are fixed in the
NG4 interface freeze and listed in the contract tests; the signatures,
`ResourceState`, `CredentialSlot`, `CredentialState` and
`CredentialOutcome` above are the part this ADR fixes. `SecretStr` is
pydantic's.

### Persistence model

ADR 0028 ("Persistence and serialisation") owns the tables and the
lease. What an adapter author needs to know:

- **The core persists adapter state; adapters are stateless.** Every
  state-changing method receives the state the core last committed
  (`prior`) and returns the complete new state. The core stores it as
  returned, in `adapter_resources` (non-secret resources) and
  `adapter_credentials` (credentials). An adapter keeps nothing between
  calls, so any worker, or the same worker after a restart, can continue.
- **One operation per scope at a time.** The core holds the lease on
  `(tenant, adapter, scope_key)` around every call that can create or
  delete something. Adapters declaring `credential_limit` are leased on
  `(tenant, adapter, "*")`, so the cap is counted while no other worker
  creates credentials for that tenant and adapter. The core refuses to
  create past the limit, counting `current` and `previous` slots of all
  scopes plus in-flight operations, and returns a non-retryable
  `AdapterError`.
- **Write-ahead for every secret-bearing call.** Before calling
  `ensure_credential`, `rotate_begin`, `rotate_finish` or
  `delete_credential`, the core commits a `credential_operations` row
  (kind, target generation, deterministic name, state `intent`) and the
  intent state (`current`/`previous` with the target slot's `name` and an
  empty `ref`). `prior` is that committed state. The name is therefore
  known and stored before the platform can possibly hold the credential.
- **Lookup rule.** When `ref` is empty the adapter looks the object up by
  `name` before creating anything; when it exists it is the object of
  this operation. `name` is unique per generation, so a retry reuses the
  same name and never creates a second credential.

### Semantics

- **Reconcile, not command.** Every `ensure_*` reads the platform's
  current state and converges on the spec; calling it twice yields
  `UNCHANGED` the second time. Identity is by durable platform ids and
  repository numeric ids, never by name or handle. `delete` and
  `delete_credential` are also idempotent (absent = `UNCHANGED`).
- **Secret issuance** (`ensure_credential` on the secret path), by the
  state of `prior.current`:
  - `ref` set and `delivered=True`: nothing to do, `UNCHANGED`,
    `credential=None`.
  - `ref` set and `delivered=False`, or `ref` empty but an object named
    `name` exists: the platform holds a credential whose values the core
    does not have (the core calls the adapter only when the values are
    not already parked, see below). The adapter deletes it and issues a
    fresh one under the same `name`, so the platform holds one
    credential per scope-generation, never two.
  - `ref` empty and nothing named `name`: create it.
  - The adapter returns the state with `current.ref` filled in and
    `delivered=False`, and the `IssuedSecret`.
- **Core sequence after the adapter returns** (the order closes the crash
  windows): (1) put `values` in the operation value store keyed by the
  operation id; (2) commit `current.ref` and the operation state
  `platform_done` in one transaction; (3) write the GitHub outputs
  (idempotent PUTs); (4) commit `delivered=True`, operation state
  `delivered`; (5) delete the parked values. A crash before (2) leaves
  the operation at `intent`: the retry issues again under the same
  name (bounded, as above). A crash after (2) and before (4) leaves
  `platform_done` with the values parked: the retry skips the adapter
  and redoes (3) to (5). The values are never stored anywhere else, and
  never in Postgres.
- **OIDC first.** `ensure_credential` returns `OidcTrust` when
  `capabilities().oidc` is true, the host's `kind` is in `oidc_hosts`
  and `GitHubHost.oidc_id_claims` is true. Otherwise it falls back to
  `IssuedSecret`. `NoCiCredential` means trustbridge provisions nothing
  for CI (Argo CD, GHCR). A `Change` with `NEEDS_MANUAL_ACTION` (and
  `credential=None`) means the adapter cannot finish via API (Docker
  Hub's OIDC connection): the core surfaces `manual_action` in the UI and
  as a check-run note, and re-runs the operation until the platform
  reports the trust. For trust, `current.delivered` is returned `True`
  (nothing secret to deliver); the core still writes the outputs
  idempotently.
- **OIDC trust claims.** `OidcTrust.claims` carries `repository_id`
  and `repository_owner_id` as strings (`repository_owner_id` only for an
  org-wide scope), plus `environment` when the scope has one. They are
  the claims GitHub documents as not changing on rename or transfer; the
  mutable `repository` / `repository_owner` names are never used for
  trust. The adapter maps them to the platform's identity-mapping syntax
  (for JFrog, claims JSON on the mapping).
- **Two-phase rotation, resumable.** The core starts a rotation by
  committing an operation `rotate` at `intent` with the intent state
  `current = Slot(generation+1, new name, ref={}, delivered=False)` and
  `previous = the old live slot`. `rotate_begin(s, prior)` creates the new
  credential under `current.name` (replacing a leftover of that name, by
  the lookup rule) while `previous` stays valid, and returns the state
  with `current.ref` set plus the `IssuedSecret`. The core parks, commits
  and delivers it as in the sequence above. Only after delivery does the
  core set `previous.retire_after = now + grace` (grace period for
  in-flight CI) and leave the operation at `awaiting_retire`. When
  `retire_after` passes, the core takes the lease and calls
  `rotate_finish(s, prior)`; the adapter deletes `previous` (by `ref`, or
  by `name` when `ref` is empty) and returns the state with
  `previous=None`. A crash or retry at any step resumes the same
  operation: a rotation request while an operation for the scope is not
  terminal resumes it and never starts generation+2 (a partial unique
  index allows one non-terminal operation per scope). For
  `rotation="single"` platforms a hard cut is unavoidable: `rotate_begin`
  replaces the secret in place, returns `previous=None`, and there is no
  finish. Harbor overlaps with two robots; Docker Hub with two OATs, so
  its `credential_limit=10` counts both.
- **Orphan sweep.** Because the name is stored before the platform holds
  the credential, a credential matching the `tb-<tenant8>-` prefix that
  appears in no `current`/`previous` slot and no non-terminal operation is
  an orphan. The reconciler lists with `list_credentials(t)` under the
  `(tenant, adapter, "*")` lease and deletes orphans with
  `delete_credential`. An operation that exhausts its attempts without
  delivery is `failed`; for an initial issue the core then deletes the
  credential (no orphan, no unusable credential kept); for a rotation the
  old credential is still valid and the new one is deleted.
- **Adapters never call GitHub.** They return `GitHubOutput`s from
  `github_outputs`; the core alone writes encrypted secrets, variables and
  environments.
- **Secrets.** `values` are `SecretStr`, never logged, never stored by
  the core except in the operation value store (a secret-store backend
  entry, deleted after delivery); the core persists only `ref` and
  `name`; `Change.detail` and exceptions must not contain them.
- **Webhooks.** `capabilities().webhook` describes how inbound events are
  authenticated. For `NONE` and `STATIC_HEADER` the payload is never
  acted on; it only triggers a re-read via the platform API. Static
  headers are compared in constant time.
- **Errors.** Adapters raise `AdapterError(retryable: bool, message)`;
  retryable errors go back to the stream's retry path. Every adapter call
  is bounded by the core with a timeout of at most half the lease TTL, so
  a holder that lost its lease cannot still be inside a call when the next
  holder starts (ADR 0028, "Persistence and serialisation").
- **Versioning.** `API_VERSION` is an integer; a breaking change bumps
  it and the core refuses adapters outside its supported range. Additive
  fields to dataclasses are non-breaking (new fields get defaults).

### Capability matrix for the MVP adapters

| Adapter | Provisions | `oidc` / `oidc_hosts` | `ci_credential` | `rotation` | Webhook auth | `github_hosts` | Privileged credential held |
| --- | --- | --- | --- | --- | --- | --- | --- |
| JFrog Artifactory | project, repos, OIDC identity mapping (`/access/api/v1/oidc`) | yes, all three kinds (Enterprise X/Enterprise+; GHES/GHE.com via `generic` provider with the host's issuer; mappings on `repository_id`/`repository_owner_id`, so GHES and GHE.com also need `oidc_id_claims`) | yes | none (trust, no secret) | `HMAC` or `STATIC_HEADER` depending on whether a webhook secret is set (JFrog is reported to sign with HMAC-SHA256 in `X-JFrog-Event-Auth`; not confirmed against current JFrog docs, NG5 fixtures settle it); trigger-only | all | admin access token |
| Harbor / MSR 4 (2.11-2.13) | project, robot account, webhook policy | no (no upstream OIDC federation); `oidc_hosts` empty | yes, robot token | `two_phase` (two robots) | `STATIC_HEADER` (Harbor webhook auth header); trigger-only | all | system or project admin robot |
| Docker Hub | repository, org access token | partial: OIDC connection is UI-only and github.com-only, so `NEEDS_MANUAL_ACTION`; `oidc_hosts = {github.com}` | yes, OAT (fallback); `credential_limit=10` | `two_phase` (watch the 10-OAT Team limit) | `NONE` (Docker Hub webhooks are unauthenticated); trigger-only | all (OAT path) | **org-owner PAT** (see G-api) |
| Argo CD | repository Secret using GitHub App credentials, AppProject, Application/ApplicationSet | n/a; `oidc_hosts` empty | **no** (`NoCiCredential`, pull-based) | none | `STATIC_HEADER` bearer, per tenant, constant-time compare; trigger-only | all | Kubernetes ServiceAccount with narrow RBAC; **raw App private key** held in the repo Secret (see G-api) |
| GHCR | nothing | no (credential is `GITHUB_TOKEN`, no trust to establish); `oidc_hosts` empty | **no** (`NoCiCredential("CI uses GITHUB_TOKEN")`) | none | n/a (GitHub events) | all; registry host derived from `GitHubHost.registry_host` | none |

`ci_credential` means trustbridge provisions a credential for CI; GHCR's
row changed from "yes, `GITHUB_TOKEN`" for consistency with that
definition. Nexus stays out of the MVP.

## Decisions for the user (G-api)

1. **The interface itself** (entry-point group, protocol, dataclasses,
   `ChangeState`, two-phase rotation, `API_VERSION`) as a public API.
   Lean: approve as written; additive changes later do not need a gate,
   breaking ones bump `API_VERSION`.
2. **Stateless adapters with prior state in, new state out**
   (`ResourceState`, `CredentialState`, `CredentialOutcome`). Trade-off:
   every method has an extra argument and a richer return type, in
   exchange for adapters that need no storage of their own and a core
   that can resume anywhere. Alternative: adapters keep their own state
   (rejected: unserialisable across workers). Lean: as written.
3. **Write-ahead operations, lease and parked values for every
   secret-bearing call** (replaces the earlier "delete and reissue when
   delivery is unrecorded" rule, which raced between workers and could
   burn Docker Hub's 10-OAT cap). Trade-off: a `credential_operations`
   table, a lease and an operation value store in the secret-store
   backend, against orphans and double issuance. Alternative: leave
   orphans to the customer. Lean: as written.
4. **Resumable two-phase rotation** (`rotate_begin`/`rotate_finish` by
   state; the core sets `retire_after` after delivery). Lean: as written.
5. **OIDC trust on `repository_id` and `repository_owner_id`**, not the
   name claims NR5 named. Trade-off: durable across rename and transfer,
   but GHES and GHE.com availability of the two claims is unconfirmed, so
   `oidc_id_claims` defaults to false there and NG4 settles it; JFrog
   accepting them in a mapping is confirmed by NG5 fixtures. Lean: as
   written.
6. **Argo CD repository credential needs a raw GitHub App private key.**
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
7. **Docker Hub needs a long-lived org-owner PAT.** An OAT cannot mint
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
8. **Capability additions**: `oidc_hosts` (per-kind OIDC support, needed
   for Docker Hub) and `credential_limit`; `GitHubHost.kind` and
   `oidc_id_claims`. Lean: as written.
9. **Tenant identity** (proposal, shared with ADR 0028): surrogate
   tenant id mapping to `(host, installation_id)`.
10. **JFrog webhook authentication** is unconfirmed against current JFrog
    docs (HMAC vs static header); settled by NG5 fixtures, no decision
    needed unless it changes the capability model.
11. The **MVP set** is JFrog, Harbor/MSR, Docker Hub, Argo CD, GHCR
    (Nexus out). Lean: approve.

## Open issues

- The value store backend (Kubernetes Secret, Vault KV) and its
  interface are named here but specified in NG3; any backend must support
  create, read and delete by operation id and encrypt at rest.
- `environment` is a mutable name; it is added to `claims` only when the
  scope has one, and GitHub documents no immutable id for it. Accepted:
  environment names are controlled by the repository owner the trust
  already names by id.
- Platform name length limits (Harbor robot names, OAT labels) may force
  an adapter to shorten `name`; the shortening must be deterministic and
  still unique per generation. NG5 contract tests check this.
- JFrog accepting `repository_id` / `repository_owner_id` as mapping
  claims, and GHES/GHE.com tokens carrying them, are unconfirmed (NG5 and
  NG4).

## Consequences

- One shape covers OIDC-capable, secret-based, manual and pull-based
  platforms, so the core has no per-vendor branches.
- Idempotency, the lease and the write-ahead row make at-least-once
  delivery and parallel workers safe for everything including secret
  issuance; the cost is every adapter doing a read before every write and
  honouring the lookup rule.
- Public API: third-party adapters are possible, and the interface can
  only grow additively. `API_VERSION` is the escape hatch.
- Contract tests per adapter run against recorded API fixtures (NG5);
  Harbor fixtures cover 2.13. They include replay tests: each method
  called twice with the same `prior`, and with `ref` emptied, must leave
  one platform object.
- Adapters that hold privileged credentials are visibly flagged
  (`needs_privileged_credential`), feeding the audit log.

## Sources

- GitHub OIDC reference (claims table including `repository_id` and
  `repository_owner_id`, customising `sub`, immutable `sub`):
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
- Detailed NR5 findings: family plan Decision log, 2026-10-31 (NR5), and
  the NG2 re-plan row (the persistence model).
