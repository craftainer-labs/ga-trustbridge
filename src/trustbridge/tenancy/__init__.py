"""Tenant context and the per-transaction row-level-security setting (ADR 0031)."""

from trustbridge.tenancy.context import current_tenant, tenant_context
from trustbridge.tenancy.hook import (
    TENANT_SETTING,
    configure_transaction_timeout,
    install_tenant_hook,
)
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
    get_tenant,
    iterate_tenants,
    list_tenants,
    tenant_session,
)
from trustbridge.tenancy.timeout_check import (
    TimeoutCheck,
    TimeoutCheckTimeout,
    TimeoutFacts,
    TimeoutGate,
    TimeoutVerdict,
    register_timeout_check,
)
from trustbridge.tenancy.timeout_check import (
    evaluate as evaluate_timeouts,
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
    "TimeoutCheck",
    "TimeoutCheckTimeout",
    "TimeoutFacts",
    "TimeoutGate",
    "TimeoutVerdict",
    "budget_left",
    "configure_transaction_timeout",
    "current_tenant",
    "evaluate",
    "evaluate_timeouts",
    "for_each_tenant",
    "get_tenant",
    "install_tenant_hook",
    "iterate_tenants",
    "list_tenants",
    "register_role_check",
    "register_timeout_check",
    "tenant_context",
    "tenant_session",
]
