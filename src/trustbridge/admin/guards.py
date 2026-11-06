"""Route guards: authentication, the rate limit, then the role check (ADR 0033 invariants 1, 14).

`guard(...)` returns the `dependencies` list of a route: `get_principal` first (authentication),
then the principal's rate-limit bucket, then the RBAC dependency. The limit runs before the role
check so that a principal over its budget costs no tenant lookup (`get_tenant`'s database read)
and no `authz.denied` work. The role dependencies are recorded in `GUARDED` and the limit
dependencies in `LIMITS` so the router-walk test can prove every admin route declares all three,
in this order. `get_principal` is cached per request, so the role check reuses its result.
"""

import hashlib
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from typing import Annotated, Any, Literal

from fastapi import Depends, HTTPException, Request
from fastapi.params import Depends as DependsParam
from fastapi.routing import APIRoute
from starlette.requests import Request as StarletteRequest
from starlette.routing import BaseRoute
from starlette.types import Receive, Scope

from trustbridge.admin.problems import AdminProblem
from trustbridge.admin.runtime import (
    DEFAULT_MAX_BODY_BYTES,
    AdminRuntime,
    admin_runtime,
    get_admin_runtime,
)
from trustbridge.audit.actions import Action
from trustbridge.audit.denial import Denial
from trustbridge.rbac import (
    Principal,
    Role,
    get_principal,
    instance_roles,
    require_role,
    tenant_roles,
)
from trustbridge.rbac.deps import (
    Authorized,
    RbacRuntime,
    client_ip,
    deny,
    get_runtime,
    parse_tenant_id,
    safe_request_id,
    tenant_not_found,
)

Bucket = Literal["read", "mutation", "export"]
GUARDED: dict[Callable[..., object], tuple[Role | None, str | None]] = {}
"""Each guard dependency and the `(role, tenant_param)` it requires (`None` role: any caller)."""


LIMITS: set[Callable[..., object]] = set()
"""Every rate-limit dependency `guard` / `guard_any_grant` built."""

LIMIT_BUCKETS: dict[Callable[..., object], Bucket] = {}
"""The bucket each rate-limit dependency counts against."""

EMITTERS: dict[Callable[..., object], Action] = {}
"""Each mutating route handler and the audit action it writes (`emits`), ADR 0032 invariant 8."""

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

INSTANCE_ONLY: set[Callable[..., object]] = set()
"""The guard dependencies built with `instance_only=True` (an instance grant on a tenant route)."""


def principal_key(iss: str | None, sub: str) -> str:
    """The bounded limiter key of a principal: a hash of the (issuer, subject) pair."""
    return hashlib.sha256(f"{iss or ''}\0{sub}".encode()).hexdigest()[:32]


def _limit(bucket: Bucket) -> Callable[..., object]:
    async def dependency(
        request: Request,
        principal: Annotated[Principal, Depends(get_principal)],
        runtime: Annotated[AdminRuntime, Depends(admin_runtime)],
    ) -> None:
        limiter = getattr(runtime.limits, bucket)
        key = principal_key(principal.iss, principal.sub)
        # From here on the caller is authenticated: a later 403 or 404 counts against this
        # principal's read bucket instead of the source address's (`problems.py`).
        request.state.admin_principal_key = key
        decision = await limiter.check_key(key)
        if not decision.allowed:
            raise AdminProblem(
                429,
                "rate-limited",
                "Rate limit exceeded",
                headers={"Retry-After": str(decision.retry_after)},
            )

    LIMITS.add(dependency)
    LIMIT_BUCKETS[dependency] = bucket
    return dependency


def emits[F: Callable[..., object]](action: Action) -> Callable[[F], F]:
    """Mark a mutating route handler as writing one `action` event (apply under the route).

    `mutations_without_emitter` fails the router walk for a mutating route without it.
    """

    def mark(handler: F) -> F:
        EMITTERS[handler] = action
        return handler

    return mark


def mutations_without_emitter(routes: Sequence[BaseRoute]) -> list[str]:
    """`"METHOD path"` of every mutating route that has no emitter or not the mutation bucket.

    A route is mutating when it answers any method outside `SAFE_METHODS`. It needs a handler
    marked with `emits` and, as its second dependency, the `mutation` rate-limit bucket.
    """
    bad: list[str] = []
    for route in routes:
        if not isinstance(route, APIRoute):
            continue
        methods = route.methods or set()
        if methods <= SAFE_METHODS:
            continue
        calls = [d.call for d in route.dependant.dependencies]
        limit = calls[1] if len(calls) > 1 else None
        bucket = None if limit is None else LIMIT_BUCKETS.get(limit)
        if route.endpoint not in EMITTERS or bucket != "mutation":
            bad.append(f"{','.join(sorted(methods))} {route.path}")
    return bad


def require_instance_role_on_tenant(
    role: Role, *, tenant_param: str
) -> Callable[..., AsyncIterator[Authorized]]:
    """Build a dependency for a `{tenant_id}` route that needs `role` at **instance scope**.

    ADR 0033 lists tenant disable and enable as "instance scope" although their path names a
    tenant. A tenant that is unknown, unparsable, or on which the caller holds no grant at all
    is a 404 with the same body (the ids stay unenumerable, and `authz.denied` records
    `tenant_not_granted`). A caller with a grant on the tenant but no instance-scope `role`
    (a tenant-scoped admin) is a 403 (`role_missing`). Only after both checks is the tenant
    context entered, as in `require_role`.
    """

    async def _dependency(
        request: Request,
        principal: Annotated[Principal, Depends(get_principal)],
        runtime: Annotated[RbacRuntime, Depends(get_runtime)],
    ) -> AsyncIterator[Authorized]:
        async def audit(
            reason: Literal["role_missing", "tenant_not_granted"], tenant: str | None
        ) -> None:
            if runtime.denials is not None:
                await runtime.denials.denied(
                    Denial(
                        sub=principal.sub,
                        iss=principal.iss,
                        reason=reason,
                        required_role=role.value,
                        scope="instance",
                        tenant_id=tenant,
                        source_ip=client_ip(request),
                        request_id=safe_request_id(request),
                    )
                )

        if tenant_param not in request.path_params:
            raise RuntimeError(f"route has no path parameter {tenant_param!r}")
        requested = parse_tenant_id(request.path_params[tenant_param])
        tenant = None if requested is None else await runtime.access.get_tenant(requested)
        if tenant is None or not tenant_roles(principal, tenant):
            await audit("tenant_not_granted", None if requested is None else str(requested))
            raise tenant_not_found(request, principal)
        if role not in instance_roles(principal):
            await audit("role_missing", str(tenant.id))
            raise deny(request, principal, role)
        with runtime.access.tenant_context(tenant.id):
            yield Authorized(principal, tenant.id)

    return _dependency


def guard(
    role: Role | None,
    bucket: Bucket = "read",
    *,
    tenant_param: str | None = None,
    instance_only: bool = False,
) -> list[DependsParam]:
    """Dependencies for a route needing `role` (None: any authenticated caller).

    `instance_only` (with `tenant_param`) needs the role at instance scope but still enters the
    tenant's context and answers 404 for a tenant the caller has no grant on.
    """
    if role is None:
        auth: Callable[..., object] = get_principal
    elif instance_only:
        if tenant_param is None:
            raise ValueError("instance_only needs a tenant_param")
        auth = require_instance_role_on_tenant(role, tenant_param=tenant_param)
        INSTANCE_ONLY.add(auth)
    else:
        auth = require_role(role, tenant_param=tenant_param)
    GUARDED[auth] = (role, tenant_param)
    return [Depends(get_principal), Depends(_limit(bucket)), Depends(auth)]


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
    return [Depends(get_principal), Depends(_limit(bucket)), Depends(any_grant)]


class _BoundedRequest(StarletteRequest):
    """A request whose body stream stops at `limit` bytes (413), however it is framed."""

    def __init__(self, scope: Scope, receive: Receive, limit: int) -> None:
        """Wrap the request's scope and receive channel."""
        super().__init__(scope, receive)
        self._limit = limit

    async def stream(self) -> AsyncGenerator[bytes]:
        """The body chunks; raises the 413 as soon as more than `limit` bytes have arrived."""
        seen = 0
        async for chunk in super().stream():
            seen += len(chunk)
            if seen > self._limit:
                raise _too_large(self._limit)
            yield chunk


def _too_large(limit: int) -> AdminProblem:
    return AdminProblem(413, "payload-too-large", f"Request body over {limit} bytes")


class BoundedRoute(APIRoute):
    """An `APIRoute` that bounds the request body (`AdminRuntime.max_body_bytes`, ADR 0033).

    A `Content-Length` over the limit is a 413 before any byte is read, a malformed one a 400,
    and a body without a length (chunked) is cut off with a 413 when it grows past the limit.
    FastAPI reads the body before the dependencies run, so the bound has to sit here.
    """

    def get_route_handler(self) -> Callable[[Request], Any]:
        """The route handler with a bounded request wrapped around it."""
        original = super().get_route_handler()

        async def handler(request: Request) -> Any:  # noqa: ANN401 -- a Starlette response
            runtime = get_admin_runtime(request.app)
            limit = DEFAULT_MAX_BODY_BYTES if runtime is None else runtime.max_body_bytes
            declared = request.headers.get("content-length")
            if declared is not None:
                if not declared.isascii() or not declared.isdigit():
                    raise AdminProblem(400, "bad-request", "Invalid Content-Length")
                if int(declared) > limit:
                    raise _too_large(limit)
            return await original(_BoundedRequest(request.scope, request.receive, limit))

        return handler


__all__ = [
    "EMITTERS",
    "GUARDED",
    "INSTANCE_ONLY",
    "LIMITS",
    "LIMIT_BUCKETS",
    "SAFE_METHODS",
    "BoundedRoute",
    "Bucket",
    "any_grant",
    "emits",
    "guard",
    "guard_any_grant",
    "mutations_without_emitter",
    "principal_key",
    "require_instance_role_on_tenant",
]
