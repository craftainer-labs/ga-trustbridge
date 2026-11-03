"""The RBAC vocabulary: roles and their ladder, grants, the principal, the tenant row.

Pure data and pure functions; no I/O, no FastAPI, nothing from `trustbridge.tenancy`.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class Role(StrEnum):
    """The four trustbridge roles (ADR 0033 decision 2)."""

    ADMIN = "admin"
    OPERATOR = "operator"
    AUDITOR = "auditor"
    VIEWER = "viewer"


# What holding a role confers (ADR 0033 "Matrix"): viewer < operator < admin, and auditor
# includes viewer. Audit is the exception to the ladder: only `auditor` confers `auditor`, so
# admin and operator never satisfy an auditor requirement.
_CONFERS: dict[Role, frozenset[Role]] = {
    Role.VIEWER: frozenset({Role.VIEWER}),
    Role.OPERATOR: frozenset({Role.OPERATOR, Role.VIEWER}),
    Role.ADMIN: frozenset({Role.ADMIN, Role.OPERATOR, Role.VIEWER}),
    Role.AUDITOR: frozenset({Role.AUDITOR, Role.VIEWER}),
}


def confers(held: Iterable[Role]) -> frozenset[Role]:
    """Every role the held roles confer (each includes itself)."""
    return frozenset().union(*(_CONFERS[role] for role in held))


@dataclass(frozen=True, slots=True)
class Grant:
    """One role at one scope: instance (`host` and `org` both None) or one organisation.

    `host` and `org` are stored lower-case: matching is case-insensitive (GitHub logins and
    host names are), the role itself is exact.
    """

    role: Role
    host: str | None = None
    org: str | None = None

    @property
    def instance(self) -> bool:
        """Whether this grant covers the whole instance."""
        return self.host is None


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller: the durable `sub`, the issuer and the grants from its token."""

    sub: str
    iss: str | None
    grants: frozenset[Grant]


@dataclass(frozen=True, slots=True)
class TenantRef:
    """The columns of `tb_list_tenants()` RBAC needs (ADR 0031): id, host and org login."""

    id: UUID
    host: str
    org: str | None


def instance_roles(principal: Principal) -> frozenset[Role]:
    """The roles the principal's instance-scope grants confer."""
    return confers(grant.role for grant in principal.grants if grant.instance)


def tenant_grants(principal: Principal, tenant: TenantRef) -> frozenset[Role]:
    """Roles the principal holds directly on `tenant`: instance grants plus matching org grants.

    A grant naming a host and org matches only a tenant whose `host` and `org` equal them
    case-insensitively; an org with no tenant grants nothing, and a grant on tenant A
    confers nothing on tenant B. The result is not yet expanded by the ladder.
    """
    host = tenant.host.lower()
    org = tenant.org.lower() if tenant.org is not None else None
    return frozenset(
        grant.role
        for grant in principal.grants
        if grant.instance or (org is not None and grant.host == host and grant.org == org)
    )


def tenant_roles(principal: Principal, tenant: TenantRef) -> frozenset[Role]:
    """Every role the principal holds on `tenant`, expanded by the ladder."""
    return confers(tenant_grants(principal, tenant))
