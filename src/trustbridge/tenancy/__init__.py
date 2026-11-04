"""Tenant context and the per-transaction row-level-security setting (ADR 0031)."""

from trustbridge.tenancy.context import current_tenant, tenant_context
from trustbridge.tenancy.hook import TENANT_SETTING, install_tenant_hook
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
    "SweepResult",
    "TenantInfo",
    "current_tenant",
    "for_each_tenant",
    "install_tenant_hook",
    "iterate_tenants",
    "list_tenants",
    "tenant_context",
    "tenant_session",
]
