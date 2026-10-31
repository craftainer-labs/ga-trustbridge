"""Provider-agnostic OIDC bearer-token validation (Authorization Code + PKCE).

Only the OpenAPI-facing piece (the OAuth2AuthorizationCodeBearer scheme, used
to render the Swagger UI login flow) is built eagerly, from static settings.
Everything that needs the network -- fetching the provider's OIDC discovery
document and JWKS -- is deferred behind an lru_cache-wrapped function, so
importing this module (and therefore app.main, and therefore every unit test
using TestClient) never requires a running OIDC provider.

Also require_roles, a Keycloak-shaped RBAC dependency (reads the
resource_access.<client>.roles claim Keycloak's client-role mapper populates --
see app.crud_1.heroes/audit for routes that use it, and
app.controllers.mock for how MODE=mock issues tokens carrying that same shape).
"""

import logging
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Annotated, Any

import httpx
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.security import OAuth2AuthorizationCodeBearer
from jwt import PyJWKClient

from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


def bearer_challenge(error: str | None = None) -> dict[str, str]:
    """The `WWW-Authenticate` header for a 401, pointing at the RFC 9728 metadata document.

    `resource_metadata` is built from configuration (settings.resource_metadata_url),
    never from the request, so a spoofed Host header cannot steer a client elsewhere.
    `error` adds the RFC 6750 error code (e.g. "invalid_token"); a request that carried
    no credentials at all gets the bare challenge, per RFC 6750 section 3.1.
    """
    challenge = f'Bearer resource_metadata="{settings.resource_metadata_url}"'
    if error is not None:
        challenge += f', error="{error}"'
    return {"WWW-Authenticate": challenge}


class _ResourceServerBearer(OAuth2AuthorizationCodeBearer):
    """OAuth2AuthorizationCodeBearer whose missing-token 401 carries the RFC 9728 challenge."""

    def make_not_authenticated_error(self) -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers=bearer_challenge(),
        )


oauth2_scheme = _ResourceServerBearer(
    authorizationUrl=settings.oidc_authorization_url,
    tokenUrl=settings.oidc_token_url,
)


@lru_cache
def _get_jwks_client() -> PyJWKClient:
    """Discover the provider's JWKS endpoint and return a caching client for it.

    A discovery-document fetch failure (provider outage, or a document missing
    `jwks_uri`) is reported as a 503 rather than left to propagate as an
    unhandled 500 -- get_current_claims logs every HTTPException it sees
    (see its own docstring), so this still lands in the same security log as a
    rejected token instead of silently bypassing it. `lru_cache` doesn't cache
    a raised exception, so a transient outage doesn't wedge this permanently.
    `# pragma: no cover` below is for tests/e2e specifically: it drives one
    live process against a real, reachable Keycloak, which can never take this
    branch -- tests/unit/test_oidc.py exercises it directly (with httpx.get
    monkeypatched to raise) and still counts toward its own 95% gate.
    """
    try:
        response = httpx.get(settings.discovery_url, timeout=10)
        response.raise_for_status()
        jwks_uri: str = response.json()["jwks_uri"]
    except (httpx.HTTPError, KeyError, ValueError) as exc:  # pragma: no cover -- see docstring
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service unavailable",
        ) from exc
    return PyJWKClient(jwks_uri, cache_keys=True)


def decode_bearer_token(token: str) -> dict[str, Any]:
    """Verify a bearer token's signature and standard claims, and return its payload.

    MODE=mock skips JWKS/network entirely and trusts whatever claims the token
    carries (see app.controllers.mock's POST /mock/token) -- a mock/test-only path,
    never used when MODE is dev or production. `# pragma: no cover` below is for
    tests/e2e specifically: it drives one live MODE=dev process, which can never
    take this branch -- tests/unit/test_oidc.py exercises it directly and still
    counts toward its own 95% gate.
    """
    try:
        if settings.mode == "mock":  # pragma: no cover
            return jwt.decode(token, options={"verify_signature": False})
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=[settings.oidc_algorithm],
            audience=settings.oidc_audience,
            issuer=settings.expected_issuer,
            options={"require": ["exp", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers=bearer_challenge("invalid_token"),
        ) from exc


def _jwks_is_warm(token: str) -> bool:
    """True when verifying `token` needs no network I/O (so it may run on the event loop).

    That holds when the JWKS client already exists (no OIDC discovery pending) and
    its JWK-set cache holds a signing key for the token's `kid`. Anything else --
    first discovery, an expired set, a rotated/unknown `kid` -- may make PyJWKClient
    fetch over blocking HTTP. A token whose header can't even be parsed is "warm":
    decode_bearer_token rejects it with a 401 before any network call.
    """
    # e2e-unreachable: prefetch_jwks warms the client at startup; covered by tests/unit/test_oidc.py
    if _get_jwks_client.cache_info().currsize == 0:  # pragma: no cover
        return False
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError:
        return True
    jwk_set = _get_jwks_client().jwk_set_cache
    cached = jwk_set.get() if jwk_set is not None else None
    # pragma: no cover below is for tests/e2e: its journeys finish well inside the JWK
    # set's cache lifespan, so the set never expires there; tests/unit/test_oidc.py
    # covers this branch directly.
    if cached is None:  # pragma: no cover
        return False
    return any(key.key_id == kid for key in cached.keys if key.public_key_use in ("sig", None))


async def decode_bearer_token_async(token: str) -> dict[str, Any]:
    """decode_bearer_token without blocking the event loop on JWKS/discovery I/O.

    The warm path (see _jwks_is_warm) is CPU-only and stays on the loop, avoiding a
    thread hop per request; the miss path runs in the threadpool. MODE=mock does no
    I/O either, so it also stays on the loop.
    """
    if settings.mode == "mock" or _jwks_is_warm(token):
        return decode_bearer_token(token)
    # e2e-unreachable: the JWKS is warm after prefetch; covered by tests/unit/test_oidc.py
    return await run_in_threadpool(decode_bearer_token, token)  # pragma: no cover


async def prefetch_jwks() -> None:
    """Warm the OIDC discovery + JWKS cache at startup (call from the app lifespan).

    No-op under MODE=mock. An unreachable IdP only logs a warning: startup must not
    depend on it, and the lazy path in get_current_claims still works (and still
    answers 503) once requests arrive.
    """
    if settings.mode == "mock":
        return

    def _prefetch() -> None:
        _get_jwks_client().get_jwk_set()

    try:
        await run_in_threadpool(_prefetch)
    # e2e-unreachable: needs an IdP outage at startup; covered by tests/unit/test_oidc.py
    except (HTTPException, jwt.PyJWTError) as exc:  # pragma: no cover
        logger.warning("JWKS prefetch failed; falling back to lazy fetch: %s", exc)


async def get_current_claims(
    request: Request, token: Annotated[str, Depends(oauth2_scheme)]
) -> dict[str, Any]:
    """FastAPI dependency: validate the request's bearer token and return its claims.

    Logs a WARNING (path and status only -- there's no verified subject to include,
    since the token itself failed to validate) on every rejection, so brute-force/
    enumeration attempts against protected routes are detectable, and an IdP-outage
    503 from _get_jwks_client is visible here too rather than only in the 500 path
    (see app.problem_details for the matching unhandled-exception log).
    """
    try:
        return await decode_bearer_token_async(token)
    except HTTPException as exc:
        logger.warning(
            "Rejected bearer token for %s (status=%d)", request.url.path, exc.status_code
        )
        raise


def require_roles(*roles: str) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Build a dependency requiring at least one of the given Keycloak client roles.

    Reads claims["resource_access"][oidc_client_id]["roles"] -- the client-role claim
    shape Keycloak's default "roles" client scope populates (see realm-export.json),
    not a claim assumed to exist on every provider's tokens (see app/README.md).
    """

    async def _dependency(
        request: Request,
        claims: Annotated[dict[str, Any], Depends(get_current_claims)],
    ) -> dict[str, Any]:
        granted = set(
            claims.get("resource_access", {}).get(settings.oidc_client_id, {}).get("roles", [])
        )
        if not granted.intersection(roles):
            logger.warning(
                "Denied role check for %s: subject=%r has none of %r",
                request.url.path,
                claims.get("sub", "unknown"),
                roles,
            )
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
        # Stashed on request.state so craftainer_crud.controllers.crud_actions's audit log can
        # name the caller without every route needing to redeclare this dependency
        # as a captured parameter just to get its return value.
        request.state.claims = claims
        return claims

    return _dependency
