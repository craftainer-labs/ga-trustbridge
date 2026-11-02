"""OIDC trust eligibility (ADR 0029 item 12, invariant A12). The core decides, not the adapter."""

from collections.abc import Iterable
from typing import Protocol

from trustbridge.adapters.interface import Capabilities, CredentialScope, Tenant


class EnvironmentInspector(Protocol):
    """Reads whether a repository's GitHub environment has protection rules (the GitHub core)."""

    async def is_protected(self, tenant: Tenant, repository_id: int, environment: str) -> bool:
        """Return True when the environment exists and has protection rules."""
        ...


async def is_trust_eligible(
    scope: CredentialScope,
    siblings: Iterable[CredentialScope],
    inspector: EnvironmentInspector,
) -> bool:
    """Return True when the scope's claims tell its permission apart from the others'.

    A `pull` scope is always eligible. A `push` or `admin` scope is eligible only when it is
    a repository scope whose `environment` is protected and used by no scope of another
    permission in the same repository. An org-wide `push` or `admin` scope never is.
    `siblings` are the tenant's other scopes (the scope itself may be included).
    """
    if scope.permission == "pull":
        return True
    if scope.repository_id is None or scope.environment is None:
        return False
    shared = any(
        other.repository_id == scope.repository_id
        and other.environment == scope.environment
        and other.permission != scope.permission
        for other in siblings
    )
    if shared:
        return False
    return await inspector.is_protected(scope.tenant, scope.repository_id, scope.environment)


def oidc_offered(caps: Capabilities, scope: CredentialScope, *, eligible: bool) -> bool:
    """Return True when the core may offer OIDC trust for this scope (invariants A6 and A7).

    Needs the capability, the host kind in `oidc_hosts`, `oidc_id_claims` on the host
    (never a fallback to the mutable name claims) and the scope's eligibility.
    """
    host = scope.github_host
    return caps.oidc and host.kind in caps.oidc_hosts and host.oidc_id_claims and eligible
