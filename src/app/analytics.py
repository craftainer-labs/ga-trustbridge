"""Product analytics wiring: the per-request GPC/DNT and consent signals for `track()`.

`craftainer_observability.analytics` (the `analytics` extra, ADR 0027) does the work:
it validates an event (name pattern, allowlisted scalar props, path-only url), applies
`ANALYTICS_*` from the environment (`ANALYTICS_ENABLED`, `_BACKEND`, `_ENDPOINT`,
`_SITE_ID`, `_HOSTNAME`, `_CONSENT`, `_RESPECT_GPC`, `_GEO`, `_PROP_KEYS`) and delivers
through a bounded, non-blocking queue. The library reads that environment itself, so
`config.py` carries no `ANALYTICS_*` fields to keep in sync with it.

What it cannot know is what a request says about tracking. `AnalyticsContextMiddleware`
reads that from the headers and wraps the request in the library's `request_context`:

- `Sec-GPC: 1` (Global Privacy Control) or `DNT: 1` (Do Not Track) sets `gpc`; the library
  then drops every event of the request while `ANALYTICS_RESPECT_GPC` is true (the default).
- `Analytics-Consent: 1` sets `consent`. It matters only with `ANALYTICS_CONSENT=opt-in`,
  where no event is recorded for a request without it. A browser never sends this header
  by itself: a front end or BFF that has collected consent adds it to its calls.

Pure ASGI, like `app.http_headers`: no `BaseHTTPMiddleware`, so the request's body and
response pass through untouched, and the `ContextVar` is visible to everything the
request runs, including sync endpoints in the threadpool.
"""

from craftainer_observability.analytics import request_context
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

CONSENT_HEADER = "Analytics-Consent"


def _signalled(headers: Headers, name: str) -> bool:
    """Whether the header `name` is present with the value `1` (the only value that counts)."""
    return headers.get(name, "").strip() == "1"


class AnalyticsContextMiddleware:
    """Pure ASGI middleware: runs each HTTP request inside `request_context(gpc=, consent=)`."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap `app`, the next layer in the ASGI middleware stack."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Set the tracking signals for this request, then delegate to the wrapped app."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        with request_context(
            gpc=_signalled(headers, "Sec-GPC") or _signalled(headers, "DNT"),
            consent=_signalled(headers, CONSENT_HEADER),
        ):
            await self.app(scope, receive, send)
