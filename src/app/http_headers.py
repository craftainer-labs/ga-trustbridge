"""RFC 8594 Sunset header support, plus baseline security response headers.

sunset() is a reusable FastAPI dependency any route can opt into to advertise
that it will stop being available at a given time -- see
app.crud_1.heroes.heroes_v1 for the applied example. add_security_headers(app)
is unconditional middleware instead of a dependency (see its own docstring for
why) applied to every response, including error responses a route's own
dependencies never run for.

`sunset()`'s `link is None` branch is `# pragma: no cover`: every call site in
this app (app.controllers.heroes_v1/heroes_v1_xml/heroes_v1_web)
passes a `link`, so a live request never reaches the no-link path.
tests/unit/test_http_headers.py exercises it directly and still counts toward
its own 95% gate.
"""

from collections.abc import Callable
from datetime import datetime
from email.utils import format_datetime

from fastapi import FastAPI, Response
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Every served HTML page (render_crud_form) loads its only script from the
# same origin (`<script src="{own_base}/components.js">`, never inline)
# and defines no other resource type -- so a locked-down default-src covers
# it without an `unsafe-inline`/`unsafe-eval` carve-out. frame-ancestors
# 'none' is CSP's own equivalent of X-Frame-Options: DENY (set alongside it
# below for the older header's wider browser support).
_CONTENT_SECURITY_POLICY = "default-src 'self'; frame-ancestors 'none'"

# FastAPI's built-in /docs and /redoc pages pull Swagger UI/Redoc from
# cdn.jsdelivr.net and run inline <script>/<style> tags -- the strict
# default-src above blocks all of that, leaving the page blank. These two
# paths get a relaxed policy instead of exempting them from CSP entirely.
_DOCS_PATHS = {"/docs", "/redoc"}
_DOCS_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' cdn.jsdelivr.net fonts.googleapis.com; "
    "img-src 'self' data: fastapi.tiangolo.com; "
    "font-src fonts.gstatic.com; "
    "frame-ancestors 'none'"
)


def sunset(at: datetime, *, link: str | None = None) -> Callable[[Response], None]:
    """Build a dependency that sets Sunset/Deprecation (and optionally Link) headers.

    `at` is rendered as an RFC 7231 HTTP-date via the stdlib, per RFC 8594's Sunset
    header format (an HTTP-date, not an IXDTF/ISO 8601 timestamp).
    """

    def _dependency(response: Response) -> None:
        response.headers["Deprecation"] = "true"
        response.headers["Sunset"] = format_datetime(at, usegmt=True)
        if link is not None:  # pragma: no branch -- see module docstring
            response.headers["Link"] = f'<{link}>; rel="sunset"'

    return _dependency


class _SecurityHeadersMiddleware:
    """Pure ASGI middleware: adds headers to "http.response.start", body untouched.

    Deliberately not FastAPI/Starlette's `@app.middleware("http")` (built on
    BaseHTTPMiddleware): that reads and re-emits the whole response body through a
    second task/memory channel, splitting a single "http.response.body" message into
    more than one -- which broke
    tests/integration/controllers/test_heroes.py::test_write_is_committed_before_the_response_is_sent,
    a regression test asserting the response body is sent in exactly one ASGI
    message. Wrapping `send` directly instead means every byte the app itself sends
    passes through unchanged.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap `app`, the next layer in the ASGI middleware stack."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Delegate to the wrapped app, adding headers only to the response start."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Content-Security-Policy"] = (
                    _DOCS_CONTENT_SECURITY_POLICY
                    if scope["path"] in _DOCS_PATHS
                    else _CONTENT_SECURITY_POLICY
                )
                headers["X-Frame-Options"] = "DENY"
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
                headers["Referrer-Policy"] = "no-referrer"
                headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
            await send(message)

        await self.app(scope, receive, _send)


def add_security_headers(app: FastAPI) -> None:
    """Register middleware that sets baseline security headers on every response.

    A dependency (like sunset() above) only runs for routes that declare it, and
    never for a response FastAPI/Starlette builds itself (404s, RFC 9457 error
    responses -- see app.problem_details). Middleware wraps every response
    regardless of which route produced it, which is what these headers need:
    Content-Security-Policy and X-Frame-Options harden the HTML pages
    app.web_components serves against injected/framed content, and
    X-Content-Type-Options stops a browser from ever re-sniffing a JSON/XML
    response body as something else. Strict-Transport-Security pins every
    origin serving this app to HTTPS for future requests (a no-op over plain
    HTTP, where browsers ignore it), Referrer-Policy stops the full request
    URL leaking to a third party via the Referer header on an outbound link,
    and Permissions-Policy denies powerful browser features this app's pages
    never use.
    """
    app.add_middleware(_SecurityHeadersMiddleware)
