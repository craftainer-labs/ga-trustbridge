"""Role-based access for the admin API (ADR 0033): claims to grants, the ladder, the dependency."""

from trustbridge.rbac.claims import PERSONA_ALIASES, build_principal, parse_grants
from trustbridge.rbac.deps import (
    Authorized,
    RbacConfig,
    RbacRuntime,
    TenantAccess,
    get_principal,
    require_role,
)
from trustbridge.rbac.model import (
    Grant,
    Principal,
    Role,
    TenantRef,
    confers,
    instance_roles,
    tenant_roles,
)

__all__ = [
    "PERSONA_ALIASES",
    "Authorized",
    "Grant",
    "Principal",
    "RbacConfig",
    "RbacRuntime",
    "Role",
    "TenantAccess",
    "TenantRef",
    "build_principal",
    "confers",
    "get_principal",
    "instance_roles",
    "parse_grants",
    "require_role",
    "tenant_roles",
]
