"""The adapter plugin interface (I2, ADR 0029): dataclasses, enums, errors and the protocol.

This module is a public API that third parties implement. Additive changes (new
dataclass fields with defaults) are non-breaking; a breaking change bumps
`API_VERSION`. Adapters never call GitHub and never persist anything themselves:
the core hands each state-changing method the state it last committed (`prior`)
and stores the state the method returns.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import ClassVar, Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import SecretStr

API_VERSION = 1
"""The adapter API version this core implements (an integer, bumped on a breaking change)."""

MIN_SUPPORTED_API_VERSION = 1
"""The oldest adapter API version this core still accepts (see `loader.check_api_version`)."""

HostKind = Literal["github.com", "ghes", "ghe.com"]
Permission = Literal["pull", "push", "admin"]

SCOPE_KEY_LENGTH = 32
"""Hex characters of the sha256 digest kept in a scope key."""

ORG_WIDE = "org"
"""The repository part of a scope key for an org-wide scope."""


# ---- hosts and scope -------------------------------------------------------
@dataclass(frozen=True, slots=True)
class GitHubHost:
    """The adapter-facing projection of `GitHubHostConfig` (ADR 0030)."""

    name: str
    kind: HostKind
    api_url: str
    oidc_issuer: str
    oidc_id_claims: bool
    registry_host: str


@dataclass(frozen=True, slots=True)
class Tenant:
    """A tenant: a surrogate id for `(host, installation_id)`."""

    id: UUID
    github_host: GitHubHost
    org: str


def scope_key(repository_id: int | None, environment: str | None, permission: Permission) -> str:
    """Return the deterministic scope key: a hex sha256 prefix of the scope's identity."""
    material = (
        f"{ORG_WIDE if repository_id is None else repository_id}|{environment or ''}|{permission}"
    )
    return sha256(material.encode()).hexdigest()[:SCOPE_KEY_LENGTH]


@dataclass(frozen=True, slots=True)
class CredentialScope:
    """What a credential is for. The core computes `key`; adapters treat it as opaque."""

    tenant: Tenant
    github_host: GitHubHost
    repository_id: int | None
    repository_owner_id: int
    environment: str | None
    permission: Permission
    key: str
    # Additive field (API_VERSION 1): True only when the core allows OIDC trust for this
    # scope (capability, host kind, `oidc_id_claims` and eligibility, invariant A12 all agree).
    # An adapter returns an `OidcTrust` exactly when it is True, otherwise an `IssuedSecret`.
    oidc_eligible: bool = False
    # Additive field (API_VERSION 1): the `RepoSpec.key` of the platform repository this
    # credential is limited to. When set, the adapter grants per repository (least privilege);
    # None keeps the project- or tenant-wide grant narrowed by claims. Not part of `key`.
    platform_repository_key: str | None = None

    @classmethod
    def build(
        cls,
        tenant: Tenant,
        *,
        repository_id: int | None,
        repository_owner_id: int,
        environment: str | None,
        permission: Permission,
    ) -> CredentialScope:
        """Build a scope, computing its key from the identity fields."""
        return cls(
            tenant=tenant,
            github_host=tenant.github_host,
            repository_id=repository_id,
            repository_owner_id=repository_owner_id,
            environment=environment,
            permission=permission,
            key=scope_key(repository_id, environment, permission),
        )


# ---- capabilities ----------------------------------------------------------
class WebhookAuth(StrEnum):
    """How inbound webhooks from the platform are authenticated."""

    HMAC = "hmac"
    STATIC_HEADER = "static_header"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class WebhookCapability:
    """Inbound webhook authentication. Non-HMAC payloads are only ever a trigger to re-read."""

    auth: WebhookAuth
    header: str | None = None
    trigger_only: bool = True


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What an adapter can do; the core checks it before any call."""

    oidc: bool
    oidc_hosts: frozenset[HostKind]
    oidc_requires_manual_step: bool
    ci_credential: bool
    rotation: Literal["none", "single", "two_phase"]
    webhook: WebhookCapability | None
    github_hosts: frozenset[HostKind]
    needs_privileged_credential: bool
    credential_limit: int | None = None


# ---- persisted state -------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ResourceState:
    """Non-secret resource (project, repository, webhook). No secrets, ever."""

    name: str
    ref: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CredentialSlot:
    """One generation of a credential."""

    generation: int
    name: str
    ref: Mapping[str, str] = field(default_factory=dict)
    delivered: bool = False
    retire_after: datetime | None = None


@dataclass(frozen=True, slots=True)
class CredentialState:
    """The persisted state of one credential scope."""

    current: CredentialSlot | None
    previous: CredentialSlot | None = None


@dataclass(frozen=True, slots=True)
class ListedCredential:
    """A trustbridge-named credential found on the platform (for the orphan sweep)."""

    name: str
    ref: Mapping[str, str] = field(default_factory=dict)


# ---- results ---------------------------------------------------------------
class ChangeState(StrEnum):
    """What a call did."""

    UNCHANGED = "unchanged"
    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"
    NEEDS_MANUAL_ACTION = "needs_manual_action"


@dataclass(frozen=True, slots=True)
class Change:
    """A change report. `detail` and `manual_action` never contain secrets."""

    state: ChangeState
    manual_action: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class Applied:
    """Result of a non-secret resource call; `state` is None after a delete."""

    change: Change
    state: ResourceState | None


@dataclass(frozen=True, slots=True)
class OidcTrust:
    """OIDC trust established on the platform. `claims` never holds `sub` or name claims."""

    issuer: str
    audience: str | None
    claims: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class IssuedSecret:
    """Secret material; never logged, parked until delivered."""

    values: Mapping[str, SecretStr]
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class NoCiCredential:
    """trustbridge provisions no CI credential for this scope."""

    reason: str


@dataclass(frozen=True, slots=True)
class CredentialOutcome:
    """Result of a credential call. The core persists `state` as returned."""

    change: Change
    state: CredentialState
    credential: OidcTrust | IssuedSecret | NoCiCredential | None


@dataclass(frozen=True, slots=True)
class GitHubOutput:
    """Something the core writes to GitHub on the adapter's behalf."""

    kind: Literal["secret", "variable", "environment"]
    name: str
    value: SecretStr | str
    repository_id: int | None
    environment: str | None


@dataclass(frozen=True, slots=True)
class Health:
    """Adapter health."""

    ok: bool
    detail: str | None = None


# ---- specs -----------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ProjectSpec:
    """Desired project. `key` is the stable identity within the tenant."""

    key: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectRef:
    """A project the tenant already has."""

    tenant: Tenant
    project: ResourceState


@dataclass(frozen=True, slots=True)
class RepoSpec:
    """Desired registry repository inside a project."""

    key: str
    package_type: str = "docker"
    repository_id: int | None = None


@dataclass(frozen=True, slots=True)
class WebhookTarget:
    """Desired platform-to-trustbridge webhook. The secret is referenced, never inlined."""

    tenant: Tenant
    url: str
    events: tuple[str, ...] = ()
    secret_ref: str | None = None


# ---- configuration and errors ---------------------------------------------
@dataclass(frozen=True, slots=True, repr=False)
class AdapterConfig:
    """Per-tenant connection data, resolved from the secret store by the core (never stored).

    `connection` holds the adapter's own settings and credentials (for example a platform
    URL, or the Argo CD read-only App's key); `privileged_credential` is the long-lived admin
    credential an adapter with `needs_privileged_credential` requires (a Docker Hub org-owner
    PAT, a JFrog admin token), or None when the tenant supplied none.
    """

    tenant: Tenant
    connection: Mapping[str, SecretStr] = field(default_factory=dict)
    privileged_credential: SecretStr | None = None

    def __repr__(self) -> str:
        """Show only the tenant and which connection keys are set, never a value."""
        return f"AdapterConfig(tenant={self.tenant.id}, keys={sorted(self.connection)})"


class AdapterError(Exception):
    """Raised by an adapter. Retryable errors are retried; others end the operation."""

    def __init__(self, retryable: bool, message: str) -> None:
        """Record whether a retry could help, and an audit-safe message (never a secret)."""
        super().__init__(message)
        self.retryable = retryable
        self.message = message


class AdapterConfigurationError(AdapterError):
    """A non-retryable configuration problem (for example a missing privileged credential)."""

    def __init__(self, message: str) -> None:
        """Create a non-retryable error."""
        super().__init__(False, message)


# ---- the protocol ----------------------------------------------------------
@runtime_checkable
class PlatformAdapter(Protocol):
    """The interface a platform adapter implements (ADR 0029)."""

    name: ClassVar[str]
    api_version: ClassVar[int]

    def capabilities(self) -> Capabilities:
        """Describe what this adapter can do."""
        ...

    async def ensure_project(
        self, t: Tenant, s: ProjectSpec, prior: ResourceState | None
    ) -> Applied:
        """Converge the project on the spec; idempotent."""
        ...

    async def ensure_repository(
        self, p: ProjectRef, s: RepoSpec, prior: ResourceState | None
    ) -> Applied:
        """Converge the repository on the spec; idempotent."""
        ...

    async def ensure_webhook(self, t: WebhookTarget, prior: ResourceState | None) -> Applied:
        """Converge the platform webhook on the target; idempotent."""
        ...

    async def delete(self, prior: ResourceState) -> Applied:
        """Delete a resource; absent means UNCHANGED."""
        ...

    async def ensure_credential(
        self, s: CredentialScope, prior: CredentialState
    ) -> CredentialOutcome:
        """Issue (or re-derive) the credential for `prior.current`."""
        ...

    async def rotate_begin(self, s: CredentialScope, prior: CredentialState) -> CredentialOutcome:
        """Create the next generation while `prior.previous` stays valid."""
        ...

    async def rotate_finish(self, s: CredentialScope, prior: CredentialState) -> CredentialOutcome:
        """Delete `prior.previous`; idempotent."""
        ...

    async def delete_credential(
        self, s: CredentialScope, prior: CredentialState
    ) -> CredentialOutcome:
        """Delete the credential(s) in `prior`; absent means UNCHANGED."""
        ...

    async def list_credentials(self, t: Tenant) -> Sequence[ListedCredential]:
        """List trustbridge-named credentials (prefix `tb-<tenant hex>-`) for the orphan sweep."""
        ...

    def github_outputs(
        self, s: CredentialScope, credential: OidcTrust | IssuedSecret | NoCiCredential
    ) -> Sequence[GitHubOutput]:
        """Return what the core should write to GitHub; a pure function of the arguments."""
        ...

    async def health_check(self) -> Health:
        """Report adapter health."""
        ...


def tenant_prefix(tenant_id: UUID) -> str:
    """Return the prefix every trustbridge-named platform credential of the tenant carries."""
    return f"tb-{tenant_id.hex}-"


def credential_name(tenant_id: UUID, key: str, generation: int) -> str:
    """Return the deterministic platform-side credential name for one generation (invariant A4)."""
    return f"{tenant_prefix(tenant_id)}{key[:16]}-g{generation}"


def resource_name(tenant_id: UUID, kind: str, key: str) -> str:
    """Return the deterministic platform-side name of a non-secret resource."""
    digest = sha256(f"{kind}|{key}".encode()).hexdigest()[:16]
    return f"{tenant_prefix(tenant_id)}{digest}"
