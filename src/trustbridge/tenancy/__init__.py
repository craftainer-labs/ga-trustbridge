"""Tenant context and the per-transaction row-level-security setting (ADR 0031)."""

from trustbridge.tenancy.context import current_tenant, tenant_context
from trustbridge.tenancy.hook import TENANT_SETTING, install_tenant_hook
from trustbridge.tenancy.role_check import (
    RoleCheck,
    RoleCheckTimeout,
    RoleFacts,
    RoleGate,
    RoleVerdict,
    budget_left,
    evaluate,
    register_role_check,
)
from trustbridge.tenancy.session import (
    SweepResult,
    TenantInfo,
    for_each_tenant,
    iterate_tenants,
    list_tenants,
    tenant_session,
)

__all__ = [
    "TENANT_SETTING",
    "RoleCheck",
    "RoleCheckTimeout",
    "RoleFacts",
    "RoleGate",
    "RoleVerdict",
    "SweepResult",
    "TenantInfo",
    "budget_left",
    "current_tenant",
    "evaluate",
    "for_each_tenant",
    "install_tenant_hook",
    "iterate_tenants",
    "list_tenants",
    "register_role_check",
    "tenant_context",
    "tenant_session",
]
