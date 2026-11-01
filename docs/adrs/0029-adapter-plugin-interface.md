# 0029. Adapter plugin interface for platform adapters (entry-point group `trustbridge.adapters`)

## Status

Accepted (2026-11-01, with the G-api decisions recorded below). This ADR
defines a public API that third parties may implement. Part of the set [0028](0028-trustbridge-architecture-and-instance-layout.md)
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
  10 OATs per org; OIDC connections are UI-only and github.com-only, so
  the adapter does not use them (see the capability matrix and item 7).
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
    oidc_id_claims: bool  # tokens from this host carry the id claims (repository_owner_id always, repository_id for repo scopes; ADR 0030)
    registry_host: str    # ghcr.io | containers.HOST | containers.SUB.ghe.com container registry host

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
    oidc_requires_manual_step: bool    # trust cannot be created via API (no MVP adapter sets this)
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
    name: str                         # tb-<tenant uuid hex, 32 chars>-<scope.key[:16]>-g<generation>; an adapter may shorten only the scope part, never the tenant id
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
    NEEDS_MANUAL_ACTION = "needs_manual_action"   # e.g. an OIDC connection only creatable in a platform UI (unused by the MVP adapters)

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
    claims: Mapping[str, str]         # claim -> value the platform must require: repository_owner_id always, repository_id when the scope has one, environment when the scope has one (A6); never `sub`, never the name claims

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
    # credential is None only when there is nothing to (re)deliver: a delivered secret that is
    # UNCHANGED, NEEDS_MANUAL_ACTION, rotate_finish, delete_credential. An OIDC scope always
    # returns its OidcTrust, also when UNCHANGED, so the core can re-derive the outputs (A1).

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
    async def list_credentials(self, t: Tenant) -> Sequence[ListedCredential]: ...   # trustbridge-named ones only (prefix tb-<tenant uuid hex>-), for the orphan sweep

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

ADR 0028 ("Persistence and serialisation") owns the tables and the lease.
Decisions that shape the adapter contract:

- **The core persists adapter state; adapters are stateless.** Every
  state-changing method receives the state the core last committed
  (`prior`) and returns the complete new state, which the core stores as
  returned (`adapter_resources` for non-secret resources,
  `adapter_credentials` for credentials). An adapter keeps nothing between
  calls, so any worker, or the same worker after a restart, can continue.
- **One operation per scope at a time.** The core serialises calls per
  `(tenant, adapter, scope)` with a lease. An adapter that declares
  `credential_limit` is serialised adapter-wide for creation, and the core
  refuses to create past the limit (counting current, previous and
  in-flight credentials) with a non-retryable `AdapterError`.
- **Write-ahead for every secret-bearing call.** The core commits the
  intent, including the deterministic platform-side `name`, before calling
  `ensure_credential`, `rotate_begin`, `rotate_finish` or
  `delete_credential`; `prior` is that committed state. The name is known
  and stored before the platform can hold the credential.
- **Lookup rule.** When `ref` is empty the adapter looks the object up by
  `name` before creating anything. `name` is unique per generation, so a
  retry reuses the name and never creates a second credential.
- **Secret values are parked, not stored.** Returned values are held in an
  operation value store (a secret-store backend entry, never Postgres)
  until the core has delivered them to GitHub, then removed.

### Semantics

- **Reconcile, not command.** Every `ensure_*` reads the platform's
  current state and converges on the spec; calling it twice yields
  `UNCHANGED` the second time. Identity is by durable platform ids and
  repository numeric ids, never by name or handle. `delete` and
  `delete_credential` are idempotent (absent = `UNCHANGED`).
- **Secret issuance.** Secret values cannot be read back, so when the
  platform already holds a credential for the target `name` whose values
  the core does not have, the adapter replaces it under the same `name`
  rather than adding another; the platform holds one credential per scope
  generation. When `current` is already delivered the result is
  `UNCHANGED`. The adapter returns the state with `current.ref` filled in
  and `delivered=False`, plus the `IssuedSecret`; the core parks,
  persists and delivers (invariants A8, and 0028 invariants 21 and 23).
- **OIDC first.** `ensure_credential` returns `OidcTrust` when
  `capabilities().oidc` is true, the host's `kind` is in `oidc_hosts`,
  `GitHubHost.oidc_id_claims` is true and the scope is **eligible for
  trust** (next bullet). Otherwise (including
  `oidc_id_claims=False`, where no `OidcTrust` and so no `claims` exist) it
  falls back to `IssuedSecret`. The core never calls `rotate_begin` for a
  scope whose current credential is an `OidcTrust`; `rotation` describes
  the `IssuedSecret` only. `NoCiCredential` means trustbridge provisions nothing for
  CI (Argo CD, GHCR). A `Change` with `NEEDS_MANUAL_ACTION` (and
  `credential=None`) means the adapter cannot finish via API (for example an
  OIDC connection that only a platform's UI can create; no MVP adapter
  uses this since Docker Hub always issues OATs, item 7): the core surfaces `manual_action` in the UI and as a
  check-run note, and re-runs the operation until the platform reports the
  trust. For trust, `current.delivered` is `True` (nothing secret to
  deliver); the core writes the outputs idempotently every time.
- **OIDC trust only where the claims tell permissions apart (G-api item
  12, option a).** The trust conditions are `repository_id` (or
  `repository_owner_id` alone for an org-wide scope) plus an optional
  environment, so scopes that differ only in `permission` would produce
  identical claims and a trust could not tell them apart. A `pull` scope is
  always eligible. A `push` or `admin` scope is eligible only when it is a
  repository scope with an `environment` that is protected (the GitHub
  environment has protection rules, as read by the core, which owns
  environments) and that no scope of another permission in the same
  repository uses; each such permission therefore has its own distinct
  protected environment. An org-wide `push` or `admin` scope is never
  eligible (it has no single repository whose environments could be
  checked). An ineligible scope is served with an `IssuedSecret`. The core
  decides eligibility, not the adapter, and re-evaluates it on every
  reconcile: when a scope stops being eligible (an environment lost its
  protection or became shared) the core switches it to an `IssuedSecret`
  and revokes the trust, by the switching rule NG4 settles (open issue).
- **OIDC trust claims.** `OidcTrust.claims` always carries
  `repository_owner_id` and, when `CredentialScope.repository_id` is not
  `None`, `repository_id`, both as strings (an org-wide scope has only
  `repository_owner_id`), plus `environment` when the scope has one: the claims
  GitHub documents as not changing on rename or transfer. The mutable
  `repository` / `repository_owner` names are never used for trust. The
  adapter maps the claims to the platform's identity-mapping syntax (for
  JFrog, claims JSON on the mapping). What the claims can and cannot tell
  apart (permissions) is the eligibility rule above.
- **Two-phase rotation, resumable.** The core starts a rotation by
  committing an intent in which `current` is the next generation (new
  name, empty `ref`) and `previous` is the live slot. `rotate_begin`
  creates the new credential while `previous` stays valid and returns it.
  After the core has delivered the new values it sets
  `previous.retire_after` (the grace period for in-flight CI), and when
  that passes calls `rotate_finish`, which deletes `previous`. A retry or
  crash at any step resumes the same operation; a rotation request while
  one is in flight resumes it and never starts a further generation. For
  `rotation="single"` platforms a hard cut is unavoidable: `rotate_begin`
  replaces the secret in place, returns `previous=None`, and there is no
  finish. Harbor overlaps with two robots; Docker Hub with two OATs, so its
  `credential_limit=10` counts both.
- **Orphan sweep.** Because the name is stored before the platform holds
  the credential, a credential carrying the full `tb-<tenant uuid hex>-`
  prefix (the whole 128-bit tenant id, so tenants sharing one platform
  account cannot match each other) that appears in no slot and no
  non-terminal operation is an orphan. The reconciler lists with
  `list_credentials(t)` and deletes orphans with `delete_credential` (the
  ordering that keeps this safe is 0028 invariant 22). An operation that
  exhausts its attempts without delivery is `failed`: an initial issue then
  has its credential deleted, and a failed rotation has its new credential
  deleted while the old one stays valid.
- **Adapters never call GitHub.** They return `GitHubOutput`s from
  `github_outputs`; the core alone writes encrypted secrets, variables and
  environments.
- **Secrets.** `values` are `SecretStr`, never logged, never stored by the
  core except in the operation value store; the core persists only `ref`
  and `name`; `Change.detail` and exceptions must not contain them.
- **Webhooks.** `capabilities().webhook` describes how inbound events are
  authenticated. For `NONE` and `STATIC_HEADER` the payload is never acted
  on; it only triggers a re-read via the platform API. Static headers are
  compared in constant time.
- **Missing privileged credential.** An adapter with
  `needs_privileged_credential` whose tenant connection data holds no such
  credential is not called for state-changing methods: the core ends the
  operation with a non-retryable configuration error, shows it as a
  condition in the UI and in the audit log, and a retry happens only after
  the tenant supplies the credential. `health_check` reports `ok=False`
  with the reason. Other adapters and other scopes are unaffected (A11).
- **Errors.** Adapters raise `AdapterError(retryable: bool, message)`;
  retryable errors go back to the retry path of ADR 0028. Every adapter
  call is bounded by the core with a timeout shorter than the lease TTL
  (0028 invariant 11).
- **Versioning.** `API_VERSION` is an integer; a breaking change bumps it
  and the core refuses adapters outside its supported range. Additive
  fields to dataclasses are non-breaking (new fields get defaults).

### Capability matrix for the MVP adapters

| Adapter | Provisions | `oidc` / `oidc_hosts` | `ci_credential` | `rotation` | Webhook auth | `github_hosts` | Privileged credential held |
| --- | --- | --- | --- | --- | --- | --- | --- |
| JFrog Artifactory | project, repos, OIDC identity mapping (`/access/api/v1/oidc`) | yes, all three kinds (Enterprise X/Enterprise+; GHES/GHE.com via `generic` provider with the host's issuer; mappings on `repository_owner_id` and, for repo scopes, `repository_id`, so GHES and GHE.com also need `oidc_id_claims`) | yes | `single` (applies to the fallback secret only; an `OidcTrust` has no secret to rotate) | `HMAC` or `STATIC_HEADER` depending on whether a webhook secret is set (JFrog is reported to sign with HMAC-SHA256 in `X-JFrog-Event-Auth`; not confirmed against current JFrog docs, NG5 fixtures settle it); trigger-only | all | admin access token |
| Harbor / MSR 4 (2.11-2.13) | project, robot account, webhook policy | no (no upstream OIDC federation); `oidc_hosts` empty | yes, robot token | `two_phase` (two robots) | `STATIC_HEADER` (Harbor webhook auth header); trigger-only | all | system or project admin robot |
| Docker Hub | repository, org access token | no: OIDC connections are UI-only and github.com-only, so the adapter never uses them; `oidc=False`, `oidc_hosts` empty, `oidc_requires_manual_step=False` | yes, OAT always (`IssuedSecret`); `credential_limit=10` | `two_phase` (watch the 10-OAT Team limit) | `NONE` (Docker Hub webhooks are unauthenticated); trigger-only | all | **tenant-supplied org-owner PAT** of a dedicated service user (item 7); `needs_privileged_credential=True` |
| Argo CD | repository Secret using the read-only App's credentials, AppProject, Application/ApplicationSet | n/a; `oidc_hosts` empty | **no** (`NoCiCredential`, pull-based) | none | `STATIC_HEADER` bearer, per tenant, constant-time compare; trigger-only | all | Kubernetes ServiceAccount with narrow RBAC; the **separate read-only GitHub App's private key** (contents: read, metadata) in the repo Secret, never trustbridge's own key (item 6) |
| GHCR | nothing | no (credential is `GITHUB_TOKEN`, no trust to establish); `oidc_hosts` empty | **no** (`NoCiCredential("CI uses GITHUB_TOKEN")`) | none | n/a (GitHub events) | all; registry host derived from `GitHubHost.registry_host` | none |

`ci_credential` means trustbridge provisions a credential for CI; GHCR's
row changed from "yes, `GITHUB_TOKEN`" for consistency with that
definition. Nexus stays out of the MVP.

## Invariants NG3 must implement and test

Each is one testable sentence. Invariants A1-A11 and A14 concern the adapter
contract (contract tests, NG5, run them against every adapter), A12 and
A13 the core (NG3 and NG4); the core
side is in ADR 0028 ("Invariants NG3 must implement and test", numbers
cited as "0028 invariant n").

- **A1.** For an OIDC scope `ensure_credential` returns the `OidcTrust`
  on every outcome except `NEEDS_MANUAL_ACTION` (which returns
  `credential=None`), including `UNCHANGED`, and `github_outputs`
  is a pure function of `(scope, credential)`, so the core can re-derive
  and re-write the outputs after a crash (0028 invariant 21).
- **A2.** Calling any state-changing method twice with the same `prior`,
  and once with `ref` emptied, leaves exactly one platform object and
  returns `UNCHANGED` on the repeat, except that a repeated issuance of a
  secret not yet delivered replaces the value under the same `name` and
  reports `UPDATED`.
- **A3.** When `ref` is empty the adapter finds an existing object by
  `name` before creating, and never creates a second object with that name.
- **A4.** A platform-side credential name contains the full tenant id and
  is unique per generation; an adapter that must shorten a name shortens
  only the scope part, deterministically (0028 invariant 24).
- **A5.** No `Change.detail`, exception message or log line produced by an
  adapter contains secret material, and no adapter makes a call to GitHub.
- **A6.** `OidcTrust.claims` contains `repository_owner_id` always,
  `repository_id` exactly when the scope's `repository_id` is not `None`,
  and `environment` exactly when the scope has one, and nothing else (never
  `sub` or the name claims),
  and the core offers trust only when the capability, the host kind,
  `oidc_id_claims` and the scope's eligibility (A12) all allow it.
- **A7.** `Capabilities.oidc_hosts` is empty when `oidc` is false and is a
  subset of `github_hosts`, and the core checks the host kind before any
  call to the adapter.
- **A8.** After a crash at any point during an issue or rotation, retries
  converge to one platform credential per scope generation with its
  outputs delivered, and once the values are parked the core does not
  call the adapter again for that operation.
- **A9.** A rotation request while an operation for the scope is not
  terminal resumes that operation and never creates a further generation;
  `rotate_finish` runs only after the new values are delivered and
  `retire_after` has passed, deletes only `previous`, and is idempotent.
- **A10.** A failed initial issue leaves no platform credential behind,
  and a failed rotation leaves the old credential valid and the new one
  deleted.
- **A11.** One scope parked at `NEEDS_MANUAL_ACTION` does not block
  operations on other scopes of the tenant.
- **A12.** (Core.) A `pull` scope is eligible for trust, a `push` or
  `admin` scope only if it is a repository scope whose `environment` is
  protected and used by no scope of another permission in that repository,
  and an org-wide `push` or `admin` scope never is; an ineligible scope
  gets an `IssuedSecret` and no `OidcTrust`, and a scope that loses
  eligibility is switched to an `IssuedSecret` at the next reconcile.
- **A13.** (Core.) For an adapter with `needs_privileged_credential`
  and no such credential in the tenant's connection data, no state-changing
  adapter method is called, the operation ends in a non-retryable
  configuration error visible as a condition, no `NEEDS_MANUAL_ACTION` is
  produced, and other scopes and adapters of the tenant are unaffected.
- **A14.** The Argo CD repository Secret holds the credentials of the
  tenant's read-only App from the tenant's connection data (its private
  key with its App id and installation id) and no key of trustbridge's own
  App, and no code path reads a signer or hands any key of trustbridge's
  own App to an adapter.

## Decisions (G-api, 2026-11-01)

Items 1-5 and 8-11 are approved as proposed; items 6, 7 and 12 are decided
as below.

1. **The interface itself** (entry-point group, protocol, dataclasses,
   `ChangeState`, two-phase rotation, `API_VERSION`) as a public API:
   approved. Additive changes need no gate; breaking ones bump
   `API_VERSION`.
2. **Stateless adapters, prior state in, new state out**: approved.
3. **Write-ahead operations, lease and parked values**: approved.
4. **Resumable two-phase rotation**: approved.
5. **OIDC trust on `repository_owner_id` and `repository_id`**, not the
   name claims: approved.
6. **Argo CD: option B.** A separate read-only GitHub App (contents: read,
   metadata), registered through the manifest flow (ADR 0030), whose key is
   the only one given to Argo CD. Option A (reuse trustbridge's key) is
   **removed**, including as a dev path, as noise (see ADR 0030 item 7);
   option C (deploy key or fine-grained token) stays rejected under EB-9.
   Judgement call: the App is registered **per tenant**, owned by the
   tenant's org, so that a key that leaks from the cluster Secret can read
   only that tenant's repositories. Invariant A14.
7. **Docker Hub: option A, always.** The adapter requires a tenant-supplied
   org-owner PAT of a dedicated service user, stored through the
   signer/secret store and rotated by the customer. It declares
   `needs_privileged_credential` and the UI shows it. There is **no**
   `NEEDS_MANUAL_ACTION` default path: a tenant without the PAT gets a
   clear configuration error and condition (A13). **Accepted exception to
   EB-9**, with its reason: Docker Hub offers no federation for minting
   OATs (an OAT cannot mint OATs, and OIDC connections are UI-only and
   github.com-only), so a long-lived owner credential is the only way to
   automate issuing. Judgement call: because the OIDC connection is
   manual-only, the adapter does not declare OIDC at all (`oidc=False`) and
   always issues OATs; the earlier `oidc_hosts = {github.com}` partial
   support is dropped. `NEEDS_MANUAL_ACTION` stays in the interface, but no
   MVP adapter uses it.
8. **Capability additions** (`oidc_hosts`, `credential_limit`,
   `GitHubHost.kind`, `oidc_id_claims`): approved.
9. **Tenant identity**: approved (shared with ADR 0028).
10. **JFrog webhook authentication**: settled by NG5 fixtures.
11. **MVP set**: JFrog, Harbor/MSR, Docker Hub, Argo CD, GHCR; Nexus out.
12. **OIDC permissions: option (a).** `push` and `admin` use OIDC only when
    each permission has a distinct protected `environment`; otherwise that
    scope falls back to an `IssuedSecret`. Judgement calls: "protected" means
    the GitHub environment has protection rules; an org-wide `push` or
    `admin` scope never uses OIDC; the core, not the adapter, decides and
    re-evaluates eligibility (invariant A12). Options (b) and (c) are not
    adopted.

Status is Accepted as of this decision although `docs/adrs/README.md` says
an ADR becomes Accepted when its implementation merges; the user asked
for Accepted now.

## Open issues

- The mechanism for passing tenant and connection data to adapter methods
  (including `delete` and `health_check`) is settled in NG5; the
  `AdapterConfig` wording in the loading paragraph is provisional until
  then, and `AdapterConfig`, `AdapterError`, `ProjectSpec` and
  `ADAPTERS_ENABLED` are defined there.
- Whether the core, a contract invariant or the adapter enforces "no
  `rotate_begin` for an `OidcTrust` scope", and the rotation behaviour when
  a scope switches between `OidcTrust` and `IssuedSecret`, is settled by NG4
  (it also covers a scope that loses eligibility, A12).
- The `api_version` compatibility rule (equality or a range, and how a
  single integer expresses it) is settled by NG4.
- The value store backend (Kubernetes Secret, Vault KV) and its interface
  are named here but specified in NG3; any backend must support create,
  read and delete by operation id and encrypt at rest (also an open issue
  in ADR 0028).
- `environment` is a mutable name; it is added to `claims` only when the
  scope has one, and GitHub documents no immutable id for it. Accepted:
  environment names are controlled by the repository owner the trust
  already names by id.
- Platform name length limits (Harbor robot names, OAT labels) may force
  an adapter to shorten `name`; invariant A4 bounds how. NG5 contract
  tests check this.
- Trusts for different permissions on one repository can both match a
  workflow that runs in a protected environment (the `pull` trust has no
  environment condition). Which credential such a workflow receives
  depends on how the platform selects among identity mappings (for
  example by name or audience); each adapter's contract fixtures (NG5)
  must show that selection, or the adapter must give each permission a
  distinct audience or mapping name.
- A Docker Hub tenant is capped at 10 OATs in total. Every scope needs one
  and a rotation needs a second briefly (the core serialises rotations
  adapter-wide), so a tenant can rotate with up to 9 scopes; with 10
  scopes rotation is refused, and a rotation that would pass the limit is
  refused with a visible
  condition while the old credential stays valid (the core's
  `credential_limit` rule); the Docker Hub documentation states this.
- Eligibility (A12) is re-evaluated at each reconcile, so a trust stays
  in force until the next reconcile after an environment loses its
  protection or becomes shared; the exposure window is the reconcile
  interval, and NG4 narrows it by re-evaluating on any GitHub event about
  environment changes that its fixtures show exists.
- How the core reads an environment's protection rules (required
  reviewers, deployment branch policy) for A12, and which of them count as
  "protected", is settled by NG4.
- How the read-only Argo CD App's key and the Docker Hub PAT reach the
  adapter (the tenant's connection data in the secret store, and the
  manifest-flow callback that writes the key there) is settled by NG4 and
  NG5 with `AdapterConfig`.
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
  Harbor fixtures cover 2.13. They include the replay tests of invariants
  A2 and A3.
- Adapters that hold privileged credentials are visibly flagged
  (`needs_privileged_credential`), feeding the audit log. Docker Hub is the
  one MVP adapter that needs a customer-supplied owner PAT: an accepted EB-9
  exception, and a Docker Hub tenant cannot be used without it.
- Argo CD costs the tenant a second, read-only App, which the manifest flow
  automates and which has its own key to rotate.
- Push and admin credentials fall back to secrets wherever a repository has
  no distinct protected environment per permission; OIDC for them is an
  opt-in through that configuration.

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
