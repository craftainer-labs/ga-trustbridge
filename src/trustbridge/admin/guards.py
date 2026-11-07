"""Route guards: authentication, the rate limit, then the role check (ADR 0033 invariants 1, 14).

`guard(...)` returns the `dependencies` list of a route: `get_principal` first (authentication),
then the principal's rate-limit bucket, then the RBAC dependency. The limit runs before the role
check so that a principal over its budget costs no tenant lookup (`get_tenant`'s database read)
and no `authz.denied` work. The role dependencies are recorded in `GUARDED` and the limit
dependencies in `LIMITS` so the router-walk test can prove every admin route declares all three,
in this order. `get_principal` is cached per request, so the role check reuses its result.
"""

import hashlib
from collections.abc import AsyncGenerator, Callable, Sequence
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
from trustbridge.rbac import Principal, Role, get_principal, require_role
from trustbridge.rbac.deps import RbacRuntime, client_ip, get_runtime, safe_request_id

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
BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
ADMIN_PREFIX = "/admin/v1"

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
    """`"METHOD path"` of every route under `/admin/v1` that breaks the mutation rules.

    A route is mutating when it answers any method outside `SAFE_METHODS`. It needs a handler
    marked with `emits` and exactly one rate-limit dependency (found by identity in
    `LIMIT_BUCKETS`, wherever it sits in the list), on the `mutation` bucket. A route under
    `/admin/v1` that is not an `APIRoute` (a `Mount`, a plain `Route`, a websocket) carries none
    of the guards and is reported too, as `"* path"`: the walk cannot see inside it.
    """
    bad: list[str] = []
    for route in routes:
        path = getattr(route, "path", "")
        if not isinstance(route, APIRoute):
            if path == ADMIN_PREFIX or path.startswith(f"{ADMIN_PREFIX}/"):
                bad.append(f"* {path}")
            continue
        methods = route.methods or set()
        if methods <= SAFE_METHODS:
            continue
        buckets = [
            LIMIT_BUCKETS[d.call] for d in route.dependant.dependencies if d.call in LIMIT_BUCKETS
        ]
        if route.endpoint not in EMITTERS or buckets != ["mutation"]:
            bad.append(f"{','.join(sorted(methods))} {path}")
    return bad


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
    else:
        auth = require_role(role, tenant_param=tenant_param, instance_only=instance_only)
        if instance_only:
            INSTANCE_ONLY.add(auth)
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

    Only `POST`, `PUT`, `PATCH` and `DELETE` are bounded; a read is passed through. The check
    runs before authentication (FastAPI reads the body before it resolves dependencies), so an
    unauthenticated caller gets the 413 or 400 without a rate-limit count or an audit event;
    that is the price of a memory bound that holds before any work is done.

    A `Content-Length` over the limit is a 413 before any byte is read, a malformed one a 400,
    and a body without a length (chunked) is cut off with a 413 when it grows past the limit.
    FastAPI reads the body before the dependencies run, so the bound has to sit here.
    """

    def get_route_handler(self) -> Callable[[Request], Any]:
        """The route handler with a bounded request wrapped around it."""
        original = super().get_route_handler()

        async def handler(request: Request) -> Any:  # noqa: ANN401 -- a Starlette response
            if request.method not in BODY_METHODS:
                return await original(request)
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
]
