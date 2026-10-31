"""Redis-backed per-route rate limiting for this app's most abuse-prone routes.

Built on the `limits` library's asyncio API (`limits.aio`: an `async+redis://` /
`async+rediss://` storage and `FixedWindowRateLimiter`), so the counter check awaits
Redis instead of blocking the event loop the way slowapi's own sync `limits`
storage would on every guarded request. slowapi is used only for its
`RateLimitExceeded` / `Limit` types. Backed by this app's already-provisioned
Redis service (`app.config.Settings.redis_url`) so the counter is shared across every
worker process, not per-process.

Deliberately per-route (`limiter.limit(...)` applied to POST /mock/token and each
resource's bulk update/delete routes -- its highest-risk, unauthenticated-or-
destructive surface, per the OWASP hardening pass this was added in), not global
`SlowAPIMiddleware`: that middleware is a `starlette.middleware.base.
BaseHTTPMiddleware`, which app.http_headers's own `_SecurityHeadersMiddleware`
docstring documents as breaking a regression test by splitting a single
"http.response.body" ASGI message into more than one. `limiter.limit(...)` needs no
middleware -- it awaits the limit check inline in the route's own call, before the
handler body runs. Decorated handlers must be coroutine functions with a
`request: Request` parameter (FastAPI injects it).

Applied to a route's update/delete/restore handler as a whole (see
craftainer_crud.controllers.crud_router), not just its bulk branch -- a single-record
edit (`?id=`) shares the same budget as a bulk one, since exempting single-
record calls would leave per-record updates/deletes unrated regardless of
how many a single client issues.

`slowapi.errors.RateLimitExceeded` is itself a `starlette.exceptions.HTTPException`
subclass (status_code=429), so app.problem_details's existing StarletteHTTPException
handler already renders it as a normal RFC 9457 problem-details body -- no separate
exception handler needed here.
"""

import functools
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from limits import parse
from limits.aio.storage import Storage
from limits.aio.strategies import FixedWindowRateLimiter
from limits.storage import storage_from_string
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from slowapi.wrappers import Limit
from starlette.requests import Request

from app.config import get_settings


class AsyncLimiter:
    """Per-route fixed-window limiter whose Redis check is awaited, not blocking.

    Keys each hit by `key_func(request)` (client address) plus the decorated
    function's `module.name`, the same scope slowapi's `Limiter` used.
    """

    def __init__(
        self,
        storage_uri: str,
        key_func: Callable[[Request], str] = get_remote_address,
    ) -> None:
        """Open the async storage for `storage_uri` and key hits with `key_func`."""
        # Sync `redis://` / `rediss://` URIs get the matching async backend.
        storage_uri = "async+" + storage_uri.removeprefix("async+")
        # `redis-py` (already a dependency) rather than limits' default `coredis`.
        options: dict[str, Any] = {"implementation": "redispy"} if "redis" in storage_uri else {}
        storage = storage_from_string(storage_uri, **options)
        # `# pragma: no cover` (here and on the other defensive checks below): a
        # misconfiguration guard tests/e2e can never trigger; tests/unit/test_rate_limit.py does.
        if not isinstance(storage, Storage):  # pragma: no cover
            msg = f"{storage_uri!r} did not resolve to an async limits storage"
            raise TypeError(msg)
        self._strategy = FixedWindowRateLimiter(storage)
        self._key_func = key_func
        self.enabled = True

    def limit(self, rate: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorate an async route so it raises `RateLimitExceeded` (429) past `rate`."""
        item = parse(rate)

        def decorator(func: Callable[..., Awaitable[object]]) -> Callable[..., Any]:
            if not inspect.iscoroutinefunction(func):  # pragma: no cover
                msg = f"{func!r} must be an async function to be rate limited"
                raise TypeError(msg)
            scope = f"{func.__module__}.{func.__name__}"
            request_index = list(inspect.signature(func).parameters).index("request")

            @functools.wraps(func)
            async def wrapper(*args: object, **kwargs: object) -> object:
                # `# pragma: no branch`: `enabled = False` is a test-only switch (unit tests).
                if self.enabled:  # pragma: no branch
                    request = (
                        kwargs["request"]
                        if "request" in kwargs
                        else args[request_index]
                        if len(args) > request_index
                        else None
                    )
                    if not isinstance(request, Request):  # pragma: no cover
                        msg = (
                            "parameter `request` must be an instance of starlette.requests.Request"
                        )
                        raise TypeError(msg)
                    key = self._key_func(request)
                    if key and not await self._strategy.hit(item, key, scope):
                        raise RateLimitExceeded(
                            Limit(item, self._key_func, None, False, None, None, None, 1, True)
                        )
                return await func(*args, **kwargs)

            return wrapper

        return decorator


settings = get_settings()

limiter = AsyncLimiter(storage_uri=settings.redis_url)
