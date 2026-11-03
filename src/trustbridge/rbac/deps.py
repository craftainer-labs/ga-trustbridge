"""The FastAPI dependency: authenticate, check the role, then (tenant routes) enter the context.

ADR 0033 "Tenant context" and ADR 0031 invariant 5: the role check and the tenant-grant
resolution run first, in their own session and transaction (`TenantAccess.list_tenants`), which
is closed before `TenantAccess.tenant_context` is entered; a handler's session opens inside it.

`require_role` returns a dependency that works on any route; mounting is NG6d's. The wiring is
`app.state.rbac = RbacRuntime(config, access)`; `TenantAccess` is the interface NG6a's
`trustbridge.tenancy` (`tb_list_tenants()` and `tenant_context`) satisfies through a thin adapter.
"""

import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Annotated, Any, Protocol
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status

from app.oidc import bearer_challenge, get_current_claims
from trustbridge.rbac.claims import build_principal
from trustbridge.rbac.model import Principal, Role, TenantRef, instance_roles, tenant_roles

logger = logging.getLogger(__name__)


class TenantAccess(Protocol):
    """What the dependency needs from the tenancy layer (ADR 0031)."""

    async def list_tenants(self) -> Sequence[TenantRef]:
        """`tb_list_tenants()` in its own short transaction, closed before it returns."""
        ...

    def tenant_context(self, tenant_id: UUID) -> AbstractContextManager[None]:
        """`trustbridge.tenancy.tenant_context(tenant_id)`."""
        ...


@dataclass(frozen=True, slots=True)
class RbacConfig:
    """The RBAC settings the dependency reads (`TrustbridgeSettings` + `OIDC_CLIENT_ID`)."""

    client_id: str
    separator: str = ":"
    alias_personas: bool = True


@dataclass(frozen=True, slots=True)
class RbacRuntime:
    """What `app.state.rbac` holds."""

    config: RbacConfig
    access: TenantAccess


def get_runtime(request: Request) -> RbacRuntime:
    """The runtime NG6d stores on `app.state.rbac`; absent means the app is misconfigured."""
    runtime = getattr(request.app.state, "rbac", None)
    if not isinstance(runtime, RbacRuntime):
        raise RuntimeError("app.state.rbac is not set")
    return runtime


async def get_principal(
    claims: Annotated[dict[str, Any], Depends(get_current_claims)],
    runtime: Annotated[RbacRuntime, Depends(get_runtime)],
) -> Principal:
    """The authenticated principal; a validated token without a `sub` is a 401."""
    config = runtime.config
    principal = build_principal(
        claims,
        client_id=config.client_id,
        separator=config.separator,
        alias_personas=config.alias_personas,
    )
    if principal is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid or expired token",
            headers=bearer_challenge("invalid_token"),
        )
    return principal


@dataclass(frozen=True, slots=True)
class Authorized:
    """A request that passed the role check; `tenant_id` is set on tenant routes."""

    principal: Principal
    tenant_id: UUID | None = None


def _deny(request: Request, principal: Principal, role: Role) -> HTTPException:
    logger.warning(
        "Denied role check for %s: sub=%r lacks %s", request.url.path, principal.sub, role.value
    )
    return HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")


def _tenant_not_found() -> HTTPException:
    """One body for an unknown tenant and a tenant the caller has no grant on."""
    return HTTPException(status.HTTP_404_NOT_FOUND, "Tenant not found")


def require_role(
    role: Role, *, tenant_param: str | None = None
) -> Callable[..., AsyncIterator[Authorized]]:
    """Build a dependency requiring `role` (or a role that includes it).

    Without `tenant_param` the route is instance-scoped: only an instance-scope grant counts,
    a tenant-scoped grant never does (403). With `tenant_param` (the path parameter holding
    the tenant id) the route is tenant-scoped: an instance grant or a grant on that tenant
    counts. A tenant that is unknown, unparsable or carries no grant for the caller is a 404
    with the same body; a grant that is too weak is a 403. Only after the check passes is the
    tenant context entered, for the rest of the request.
    """

    async def _dependency(
        request: Request,
        principal: Annotated[Principal, Depends(get_principal)],
        runtime: Annotated[RbacRuntime, Depends(get_runtime)],
    ) -> AsyncIterator[Authorized]:
        if tenant_param is None:
            if role not in instance_roles(principal):
                raise _deny(request, principal, role)
            yield Authorized(principal)
            return
        tenant = await _find_tenant(request.path_params.get(tenant_param), runtime)
        held = tenant_roles(principal, tenant) if tenant is not None else frozenset()
        if tenant is None or not held:
            raise _tenant_not_found()
        if role not in held:
            raise _deny(request, principal, role)
        with runtime.access.tenant_context(tenant.id):
            yield Authorized(principal, tenant.id)

    return _dependency


async def _find_tenant(raw: object, runtime: RbacRuntime) -> TenantRef | None:
    """The tenant row for a path value (its own closed session), or None."""
    try:
        tenant_id = UUID(str(raw))
    except ValueError:
        return None
    for tenant in await runtime.access.list_tenants():
        if tenant.id == tenant_id:
            return tenant
    return None


__all__ = [
    "Authorized",
    "RbacConfig",
    "RbacRuntime",
    "TenantAccess",
    "get_principal",
    "get_runtime",
    "require_role",
]
