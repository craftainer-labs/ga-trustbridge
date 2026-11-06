"""Problem Details for the admin API: stable `type` URIs and the rejected-request limit.

ADR 0004 gives every error an `application/problem+json` body; the template's handlers write
`type: about:blank`. For paths under `/admin/v1` this module wraps them so `type` is the stable
URI `tag:trustbridge,2026:problem/<class>` (one class per kind of error, listed in
`admin/README.md`), and counts rejected requests (ADR 0033 decision 5): a 401 (no or invalid
credentials) and a 404 for an unknown route count against the source address's `anonymous`
bucket; a 403 or 404 for an authenticated principal counts against that principal's `read`
bucket (the guard recorded its key). Over the limit the answer becomes a 429.
"""

import json
from collections.abc import Awaitable, Callable, Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.problem_details import ProblemDetailResponse
from trustbridge.admin import metrics
from trustbridge.admin.runtime import get_admin_runtime

BASE_PATH = "/admin/v1"
TYPE_PREFIX = "tag:trustbridge,2026:problem/"
CLASSES: Mapping[int, str] = {
    400: "bad-request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not-found",
    409: "conflict",
    422: "validation",
    429: "rate-limited",
    500: "internal",
    503: "unavailable",
}
"""The problem class per status; `AdminProblem` may name a more specific one."""
_REJECTED = frozenset({401, 403, 404})
_DROPPED_HEADERS = frozenset({"content-length", "content-type"})


def problem_type(kind: str) -> str:
    """The `type` URI of a problem class."""
    return f"{TYPE_PREFIX}{kind}"


class AdminProblem(StarletteHTTPException):
    """An HTTP error of a specific problem class (for example `invalid-cursor`)."""

    def __init__(
        self,
        status_code: int,
        kind: str,
        detail: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        """Keep the class name beside the status."""
        super().__init__(status_code, detail, dict(headers) if headers else None)
        self.kind = kind


def _is_admin(request: Request) -> bool:
    return request.url.path == BASE_PATH or request.url.path.startswith(BASE_PATH + "/")


def _retyped(response: JSONResponse, kind: str) -> ProblemDetailResponse:
    """The template's problem response with `type` replaced."""
    body = json.loads(bytes(response.body))
    body["type"] = problem_type(kind)
    headers = {k: v for k, v in response.headers.items() if k.lower() not in _DROPPED_HEADERS}
    return ProblemDetailResponse(content=body, status_code=response.status_code, headers=headers)


async def _rate_limited(request: Request) -> ProblemDetailResponse | None:
    """A 429 when the rejected request pushes its bucket over, else None.

    An authenticated principal's 403 or 404 counts against its own `read` bucket, so callers
    behind one address (a NAT, the BFF) do not use up each other's budget; everything else
    (401, a route that does not exist) counts against the source address.
    """
    runtime = get_admin_runtime(request.app)
    if runtime is None:
        return None
    key = getattr(request.state, "admin_principal_key", None)
    if key is not None:  # set by the guard, which runs after authentication
        decision = await runtime.limits.read.check_key(key)
    else:
        host = request.client.host if request.client else None
        decision = await runtime.limits.anonymous.check(host)
    if decision.allowed:
        return None
    return ProblemDetailResponse(
        content={
            "type": problem_type("rate-limited"),
            "title": "Too Many Requests",
            "status": 429,
            "detail": "Too many rejected requests; retry later",
            "instance": request.url.path,
        },
        status_code=429,
        headers={"Retry-After": str(decision.retry_after)},
    )


def install_problem_types(app: FastAPI) -> None:
    """Wrap the app's HTTPException, validation and unhandled-error handlers for admin paths."""
    originals = dict(app.exception_handlers)

    def wrap(
        exc_class: type[Exception], kind_of: Callable[[Exception], str]
    ) -> Callable[[Request, Exception], Awaitable[JSONResponse]]:
        original = originals[exc_class]

        async def handler(request: Request, exc: Exception) -> JSONResponse:
            response = await original(request, exc)
            if not _is_admin(request):
                return response  # type: ignore[no-any-return]
            if response.status_code in _REJECTED:
                limited = await _rate_limited(request)
                if limited is not None:
                    metrics.rejected_limited.add(1)
                    return limited
            return _retyped(response, kind_of(exc))

        return handler

    def http_kind(exc: Exception) -> str:
        if isinstance(exc, AdminProblem):
            return exc.kind
        status = exc.status_code if isinstance(exc, StarletteHTTPException) else 500
        return CLASSES.get(status, "error")

    app.exception_handlers[StarletteHTTPException] = wrap(StarletteHTTPException, http_kind)
    app.exception_handlers[RequestValidationError] = wrap(
        RequestValidationError, lambda _exc: "validation"
    )
    if Exception in originals:
        app.exception_handlers[Exception] = wrap(Exception, lambda _exc: "internal")
    app.middleware_stack = None
