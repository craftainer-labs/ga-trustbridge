"""Route guards: authentication, the role check, then the rate limit (ADR 0033 invariants 1, 14).

`guard(...)` returns the `dependencies` list of a route: the RBAC dependency first (it
authenticates), then the principal's rate-limit bucket. Each dependency is recorded in
`GUARDED` so the router-walk test can prove every admin route declares one.
"""

import hashlib
from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request
from fastapi.params import Depends as DependsParam

from trustbridge.admin.problems import AdminProblem
from trustbridge.admin.runtime import AdminRuntime, admin_runtime
from trustbridge.audit.denial import Denial
from trustbridge.rbac import Principal, Role, get_principal, require_role
from trustbridge.rbac.deps import RbacRuntime, client_ip, get_runtime, safe_request_id

Bucket = Literal["read", "mutation", "export"]
GUARDED: dict[Callable[..., object], tuple[Role | None, str | None]] = {}
"""Each guard dependency and the `(role, tenant_param)` it requires (`None` role: any caller)."""


def principal_key(sub: str) -> str:
    """The bounded limiter key of a principal."""
    return hashlib.sha256(sub.encode()).hexdigest()[:32]


def _limit(bucket: Bucket) -> Callable[..., object]:
    async def dependency(
        principal: Annotated[Principal, Depends(get_principal)],
        runtime: Annotated[AdminRuntime, Depends(admin_runtime)],
    ) -> None:
        limiter = getattr(runtime.limits, bucket)
        decision = await limiter.check_key(principal_key(principal.sub))
        if not decision.allowed:
            raise AdminProblem(
                429,
                "rate-limited",
                "Rate limit exceeded",
                headers={"Retry-After": str(decision.retry_after)},
            )

    return dependency


def guard(
    role: Role | None, bucket: Bucket = "read", *, tenant_param: str | None = None
) -> list[DependsParam]:
    """Dependencies for a route needing `role` (None: any authenticated caller)."""
    if role is None:
        auth: Callable[..., object] = get_principal
    else:
        auth = require_role(role, tenant_param=tenant_param)
    GUARDED[auth] = (role, tenant_param)
    return [Depends(auth), Depends(_limit(bucket))]


async def any_grant(
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
    runtime: Annotated[RbacRuntime, Depends(get_runtime)],
) -> None:
    """For a list filtered per grants: every role confers `viewer`, so any grant will do.

    A principal with no grant at all is a 403 and an `authz.denied` event, like a missing role.
    """
    if principal.grants:
        return
    if runtime.denials is not None:
        await runtime.denials.denied(
            Denial(
                sub=principal.sub,
                iss=principal.iss,
                reason="role_missing",
                required_role=Role.VIEWER.value,
                scope="instance",
                tenant_id=None,
                source_ip=client_ip(request),
                request_id=safe_request_id(request),
            )
        )
    raise HTTPException(403, "Insufficient role")


def guard_any_grant(bucket: Bucket = "read") -> list[DependsParam]:
    """Dependencies for a route whose result is filtered by the caller's grants."""
    GUARDED[any_grant] = (Role.VIEWER, "per-grants")
    return [Depends(any_grant), Depends(_limit(bucket))]


__all__ = ["GUARDED", "Bucket", "any_grant", "guard", "guard_any_grant", "principal_key"]
