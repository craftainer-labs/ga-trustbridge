"""Unauthenticated `/.well-known/` documents this service publishes about itself.

- `oauth-protected-resource` (RFC 9728): tells an OAuth client which authorization server
  issues tokens for this API, how to present them and which scopes exist. Always served.
  The same URL is what `app.oidc.bearer_challenge` puts in a 401's `WWW-Authenticate`.
- `security.txt` (RFC 9116): served only when `SECURITY_TXT_CONTACT` is set, since an
  instance must choose its own reporting channel. `config.Settings` validates the fields
  (a contact needs an `Expires` within a year) before this module ever runs.

The RFC 9727 `api-catalog` is mounted in `app.main` from `craftainer_crud`. Every route here
is public, outside the OpenAPI schema, and passes through the security-headers middleware
like any other response.
"""

from datetime import UTC
from typing import Any

from fastapi import APIRouter, Response
from fastapi.responses import PlainTextResponse

from app.config import Settings

_CACHE_CONTROL = "public, max-age=3600"


def protected_resource_metadata(settings: Settings) -> dict[str, Any]:
    """The RFC 9728 metadata document, built from configuration alone."""
    return {
        "resource": settings.resource_url,
        "authorization_servers": [settings.expected_issuer],
        "scopes_supported": settings.oidc_scopes_supported,
        "bearer_methods_supported": ["header"],
        "resource_name": settings.app_name,
    }


def render_security_txt(settings: Settings) -> str | None:
    """The RFC 9116 file body, or None when no contact is configured."""
    if settings.security_txt_contact is None or settings.security_txt_expires is None:
        return None
    expires = settings.security_txt_expires.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [f"Contact: {settings.security_txt_contact}", f"Expires: {expires}"]
    optional = (
        ("Preferred-Languages", settings.security_txt_preferred_languages),
        ("Policy", settings.security_txt_policy),
        ("Canonical", settings.security_txt_canonical),
    )
    lines.extend(f"{field}: {value}" for field, value in optional if value is not None)
    return "\n".join(lines) + "\n"


def build_well_known_router(settings: Settings) -> APIRouter:
    """Router for the documents above; the paths are absolute, so mount with no prefix."""
    router = APIRouter(include_in_schema=False)

    @router.get("/.well-known/oauth-protected-resource")
    def oauth_protected_resource(response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = _CACHE_CONTROL
        return protected_resource_metadata(settings)

    security_txt = render_security_txt(settings)
    if security_txt is not None:

        @router.get("/.well-known/security.txt")
        def security_txt_document() -> PlainTextResponse:
            return PlainTextResponse(security_txt, headers={"Cache-Control": _CACHE_CONTROL})

    return router
