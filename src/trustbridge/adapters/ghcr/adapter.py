"""The GHCR adapter (NG5e, ADR 0029): GitHub's own registry, so nothing is provisioned over a wire.

The adapter makes no network call. GHCR packages are created by the first push, a package's
repository access ("Manage Actions access") has no REST endpoint, and the Packages REST API takes
a classic personal access token or OAuth token, which trustbridge never holds (see `README.md`).
It therefore only records names, never creates or deletes anything on GHCR, and reports the one
UI-only step as `NEEDS_MANUAL_ACTION`.
"""

import re
from collections.abc import Sequence
from dataclasses import replace
from typing import ClassVar

from trustbridge.adapters.interface import (
    API_VERSION,
    AdapterConfig,
    AdapterError,
    Applied,
    Capabilities,
    Change,
    ChangeState,
    CredentialOutcome,
    CredentialScope,
    CredentialSlot,
    CredentialState,
    GitHubOutput,
    Health,
    HostKind,
    IssuedSecret,
    ListedCredential,
    NoCiCredential,
    OidcTrust,
    ProjectRef,
    ProjectSpec,
    RepoSpec,
    ResourceState,
    Tenant,
    WebhookTarget,
    resource_name,
)

PACKAGE_TYPES = frozenset({"docker", "container"})
"""The `RepoSpec.package_type` values that mean a container package."""

_PACKAGE_NAME = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?")
"""A single-segment, lowercase image name (GHCR image references are lowercase)."""


def namespace_of(tenant: Tenant) -> str:
    """Return the GHCR namespace of a tenant: its GitHub org, lowercased."""
    return tenant.org.lower()


def image_of(registry_host: str, namespace: str, package: str) -> str:
    """Return the image path (without a tag) of a package."""
    return f"{registry_host}/{namespace}/{package}"


def repository_manual_action(image: str, repository_id: int) -> str:
    """Return the instruction for the step GitHub offers only in its web UI."""
    return (
        f"Open the package settings of {image} and, under Manage Actions access, add the "
        f"repository with id {repository_id} with the Write role (or publish the image once "
        "from that repository's workflow with GITHUB_TOKEN and `packages: write`, which links it "
        "automatically). GitHub has no REST endpoint for this step."
    )


class GhcrAdapter:
    """Records the GHCR namespace and package names; credentials are `GITHUB_TOKEN` in CI."""

    name: ClassVar[str] = "ghcr"
    api_version: ClassVar[int] = API_VERSION

    def __init__(self, config: AdapterConfig) -> None:
        """Keep the per-tenant config (it holds no GHCR connection data)."""
        self._config = config

    # ---- configuration ---------------------------------------------------
    def capabilities(self) -> Capabilities:
        """Describe GHCR: no OIDC trust, no CI credential, no rotation, no webhooks, all hosts."""
        kinds: frozenset[HostKind] = frozenset({"github.com", "ghes", "ghe.com"})
        return Capabilities(
            oidc=False,
            oidc_hosts=frozenset(),
            oidc_requires_manual_step=False,
            ci_credential=False,
            rotation="none",
            webhook=None,
            github_hosts=kinds,
            needs_privileged_credential=False,
        )

    # ---- projects --------------------------------------------------------
    async def ensure_project(
        self, t: Tenant, s: ProjectSpec, prior: ResourceState | None
    ) -> Applied:
        """Record the tenant's namespace (its org); nothing exists to create on GHCR."""
        ref = {
            "kind": "namespace",
            "namespace": namespace_of(t),
            "registry_host": t.github_host.registry_host,
        }
        name = prior.name if prior is not None else resource_name(t.id, "project", s.key)
        return Applied(Change(self._recorded(prior, ref)), ResourceState(name=name, ref=ref))

    # ---- repositories ----------------------------------------------------
    async def ensure_repository(
        self, p: ProjectRef, s: RepoSpec, prior: ResourceState | None
    ) -> Applied:
        """Record the container package name; repository access is a manual step."""
        if s.package_type.lower() not in PACKAGE_TYPES:
            raise AdapterError(False, "GHCR hosts container packages only")
        if _PACKAGE_NAME.fullmatch(s.key) is None:
            msg = "package name must be lowercase letters, digits, '.', '_' and '-' (one segment)"
            raise AdapterError(False, msg)
        tenant = p.tenant
        namespace = p.project.ref.get("namespace") or namespace_of(tenant)
        host = p.project.ref.get("registry_host") or tenant.github_host.registry_host
        image = image_of(host, namespace, s.key)
        ref = {"kind": "package", "namespace": namespace, "package": s.key, "image": image}
        name = prior.name if prior is not None else resource_name(tenant.id, "repository", image)
        state = ResourceState(name=name, ref=ref)
        if s.repository_id is None:
            return Applied(Change(self._recorded(prior, ref)), state)
        change = Change(
            ChangeState.NEEDS_MANUAL_ACTION,
            manual_action=repository_manual_action(image, s.repository_id),
        )
        return Applied(change, state)

    @staticmethod
    def _recorded(prior: ResourceState | None, ref: dict[str, str]) -> ChangeState:
        if prior is None or not prior.ref:
            return ChangeState.CREATED
        return ChangeState.UNCHANGED if dict(prior.ref) == ref else ChangeState.UPDATED

    # ---- webhooks --------------------------------------------------------
    async def ensure_webhook(self, t: WebhookTarget, prior: ResourceState | None) -> Applied:
        """Refuse: GHCR has no webhooks of its own (GitHub delivers package events)."""
        del t, prior
        raise AdapterError(False, "GHCR has no webhooks; GitHub delivers package events")

    # ---- deletion --------------------------------------------------------
    async def delete(self, prior: ResourceState) -> Applied:
        """Forget the record; a package is never deleted (its images are not the adapter's)."""
        del prior
        change = Change(
            ChangeState.UNCHANGED,
            detail="GHCR packages are left in place; trustbridge never deletes them",
        )
        return Applied(change, None)

    # ---- credentials -----------------------------------------------------
    async def ensure_credential(
        self, s: CredentialScope, prior: CredentialState
    ) -> CredentialOutcome:
        """Report that CI uses `GITHUB_TOKEN`; per-package scopes name the package grant."""
        slot = self._current(prior)
        if s.platform_repository_key is None:
            reason = (
                "CI uses GITHUB_TOKEN with `packages: write` (or read); packages published by "
                "the workflow grant their repository access automatically"
            )
        else:
            reason = (
                f"CI uses GITHUB_TOKEN with `packages: write` (or read); the grant is the "
                f"package {s.platform_repository_key!r} only (see its Manage Actions access)"
            )
        state = CredentialState(current=replace(slot, delivered=True), previous=prior.previous)
        change = ChangeState.UNCHANGED if slot.delivered else ChangeState.CREATED
        return CredentialOutcome(Change(change), state, NoCiCredential(reason))

    async def rotate_begin(self, s: CredentialScope, prior: CredentialState) -> CredentialOutcome:
        """Nothing to rotate: `GITHUB_TOKEN` is minted per job."""
        return await self.ensure_credential(s, prior)

    async def rotate_finish(self, s: CredentialScope, prior: CredentialState) -> CredentialOutcome:
        """Nothing was created, so nothing is left to retire."""
        del s
        state = CredentialState(current=prior.current)
        return CredentialOutcome(Change(ChangeState.UNCHANGED), state, None)

    async def delete_credential(
        self, s: CredentialScope, prior: CredentialState
    ) -> CredentialOutcome:
        """Nothing exists on GHCR for a credential; absent means UNCHANGED."""
        del s, prior
        return CredentialOutcome(Change(ChangeState.UNCHANGED), CredentialState(current=None), None)

    async def list_credentials(self, t: Tenant) -> Sequence[ListedCredential]:
        """GHCR holds no trustbridge-named credentials."""
        del t
        return []

    @staticmethod
    def _current(prior: CredentialState) -> CredentialSlot:
        if prior.current is None:
            raise AdapterError(False, "no current credential slot")
        return prior.current

    def github_outputs(
        self, s: CredentialScope, credential: OidcTrust | IssuedSecret | NoCiCredential
    ) -> Sequence[GitHubOutput]:
        """Write nothing to GitHub: the job's own `GITHUB_TOKEN` needs no secret or variable."""
        del s, credential
        return []

    async def health_check(self) -> Health:
        """Always healthy: the adapter has no connection to lose."""
        return Health(ok=True, detail="GHCR needs no connection data")
