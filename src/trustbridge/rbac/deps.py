"""The FastAPI dependency: authenticate, check the role, then (tenant routes) enter the context.

ADR 0033 "Tenant context" and ADR 0031 invariant 5: the role check and the tenant-grant
resolution run first, in their own session and transaction (`TenantAccess.get_tenant`), which
is closed before `TenantAccess.tenant_context` is entered; a handler's session opens inside it.

`require_role` returns a dependency that works on any route; mounting is NG6d's. The wiring is
`app.state.rbac = RbacRuntime(config, access)`; `TenantAccess` is the interface NG6a's
`trustbridge.tenancy` (`tb_list_tenants()`/by-id lookup and `tenant_context`) satisfies through a
thin adapter.
"""

import ipaddress
import logging
import re
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status

from app.oidc import bearer_challenge, get_current_claims
from trustbridge.audit.denial import Denial, DenialSink
from trustbridge.rbac.claims import build_principal
from trustbridge.rbac.model import Principal, Role, TenantRef, instance_roles, tenant_roles

logger = logging.getLogger(__name__)
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")


class TenantAccess(Protocol):
    """What the dependency needs from the tenancy layer (ADR 0031)."""

    async def get_tenant(self, tenant_id: UUID) -> TenantRef | None:
        """The tenant row by id (None if absent), read in its own short transaction.

        The session and transaction are closed before it returns.
        """
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
    denials: DenialSink | None = None


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


def client_ip(request: Request) -> str | None:
    """The ASGI client address (already rewritten for trusted proxies), if it is an IP."""
    host = request.client.host if request.client else None
    try:
        return None if host is None or "%" in host else str(ipaddress.ip_address(host))
    except ValueError:
        return None


def safe_request_id(request: Request) -> str | None:
    """The caller's `X-Request-ID` when it is a short, plain token; anything else is dropped."""
    value = request.headers.get("x-request-id")
    return value if value is not None and _REQUEST_ID.fullmatch(value) else None


def deny(request: Request, principal: Principal, role: Role) -> HTTPException:
    """The 403 for a role that is not enough (logged)."""
    logger.warning(
        "Denied role check for %s: sub=%r lacks %s", request.url.path, principal.sub, role.value
    )
    return HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")


def tenant_not_found(request: Request, principal: Principal) -> HTTPException:
    """The 404: one body for an unknown tenant or one the caller has no grant on."""
    logger.warning(
        "Tenant not found or not granted for %s: sub=%r", request.url.path, principal.sub
    )
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

    Each denial of an authenticated principal is first handed to `RbacRuntime.denials` (the
    `authz.denied` audit event, ADR 0032), which runs before the response is chosen; if it
    fails, the request fails with it.
    """

    async def _dependency(
        request: Request,
        principal: Annotated[Principal, Depends(get_principal)],
        runtime: Annotated[RbacRuntime, Depends(get_runtime)],
    ) -> AsyncIterator[Authorized]:
        async def audit(
            reason: Literal["role_missing", "tenant_not_granted"], tenant_id: str | None
        ) -> None:
            if runtime.denials is not None:
                await runtime.denials.denied(
                    Denial(
                        sub=principal.sub,
                        iss=principal.iss,
                        reason=reason,
                        required_role=role.value,
                        scope="instance" if tenant_param is None else "tenant",
                        tenant_id=tenant_id,
                        source_ip=client_ip(request),
                        request_id=safe_request_id(request),
                    )
                )

        if tenant_param is None:
            if role not in instance_roles(principal):
                await audit("role_missing", None)
                raise deny(request, principal, role)
            yield Authorized(principal)
            return
        if tenant_param not in request.path_params:
            raise RuntimeError(f"route has no path parameter {tenant_param!r}")
        requested = parse_tenant_id(request.path_params[tenant_param])
        tenant = None if requested is None else await runtime.access.get_tenant(requested)
        held = tenant_roles(principal, tenant) if tenant is not None else frozenset()
        if tenant is None or not held:
            await audit("tenant_not_granted", None if requested is None else str(requested))
            raise tenant_not_found(request, principal)
        if role not in held:
            await audit("role_missing", str(tenant.id))
            raise deny(request, principal, role)
        with runtime.access.tenant_context(tenant.id):
            yield Authorized(principal, tenant.id)

    return _dependency


def parse_tenant_id(raw: object) -> UUID | None:
    """The tenant id of a path value, or None when it is not the canonical spelling."""
    try:
        tenant_id = UUID(str(raw))
    except ValueError:
        return None
    if str(raw).lower() != str(tenant_id):  # one spelling per tenant: no braces, urn: or bare hex
        return None
    return tenant_id


__all__ = [
    "Authorized",
    "Denial",
    "DenialSink",
    "RbacConfig",
    "RbacRuntime",
    "TenantAccess",
    "client_ip",
    "deny",
    "get_principal",
    "get_runtime",
    "parse_tenant_id",
    "require_role",
    "safe_request_id",
    "tenant_not_found",
]
