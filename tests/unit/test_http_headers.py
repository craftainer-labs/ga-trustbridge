"""Unit test: the sunset() dependency and add_security_headers() middleware."""

from datetime import UTC, datetime

from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from app.http_headers import add_security_headers, sunset


def test_sunset_sets_deprecation_and_sunset_headers() -> None:
    """sunset() without a link sets Deprecation/Sunset but no Link header."""
    response = Response()
    sunset(datetime(2027, 1, 1, tzinfo=UTC))(response)
    assert response.headers["Deprecation"] == "true"
    assert response.headers["Sunset"] == "Fri, 01 Jan 2027 00:00:00 GMT"
    assert "Link" not in response.headers


def test_sunset_sets_link_header_when_given() -> None:
    """sunset(..., link=...) also sets a Link header with rel="sunset"."""
    response = Response()
    sunset(datetime(2027, 1, 1, tzinfo=UTC), link="/heroes")(response)
    assert response.headers["Link"] == '</heroes>; rel="sunset"'


def test_add_security_headers_sets_headers_on_every_response() -> None:
    """add_security_headers(app) sets CSP/X-Frame-Options/X-Content-Type-Options."""
    app = FastAPI()

    @app.get("/ok")
    def ok() -> dict[str, bool]:
        return {"ok": True}

    add_security_headers(app)
    response = TestClient(app).get("/ok")
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Strict-Transport-Security"] == "max-age=63072000; includeSubDomains"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Permissions-Policy"] == "geolocation=(), microphone=(), camera=()"


def test_add_security_headers_covers_error_responses_too() -> None:
    """The headers are also present on a response FastAPI builds itself (a 404)."""
    app = FastAPI()
    add_security_headers(app)
    response = TestClient(app).get("/does-not-exist")
    assert response.status_code == 404
    assert response.headers["X-Frame-Options"] == "DENY"


def test_docs_page_gets_a_relaxed_csp_that_allows_swagger_ui_assets() -> None:
    """/docs needs jsdelivr script/style + inline tags, so it gets its own CSP."""
    app = FastAPI()
    add_security_headers(app)
    response = TestClient(app).get("/docs")
    csp = response.headers["Content-Security-Policy"]
    # codeql[py/incomplete-url-substring-sanitization] -- test assertion on the app's
    # own hardcoded CSP string (see http_headers.py), not a security check on an
    # untrusted URL.
    assert "cdn.jsdelivr.net" in csp
    assert "'unsafe-inline'" in csp
    assert "frame-ancestors 'none'" in csp


def test_other_routes_keep_the_strict_csp_even_when_docs_is_relaxed() -> None:
    """Relaxing /docs must not weaken the default policy for every other route."""
    app = FastAPI()

    @app.get("/ok")
    def ok() -> dict[str, bool]:
        return {"ok": True}

    add_security_headers(app)
    response = TestClient(app).get("/ok")
    csp = response.headers["Content-Security-Policy"]
    assert csp == "default-src 'self'; frame-ancestors 'none'"
