"""HTTP routes for POST /mock/token -- only mounted when MODE=mock (see app.main).

Issues an unsigned-in-practice JWT shaped like a real Keycloak token (the same
resource_access.<client>.roles claim app.oidc.require_roles reads), so RBAC is
exercisable without a running Keycloak. app.oidc.decode_bearer_token's own
MODE=mock branch never verifies the signature, so the signing key here is a fixed,
non-secret, local-only value -- never used outside MODE=mock.

tests/e2e/conftest.py runs one parametrized leg fully under MODE=mock, so this
route is exercised through a real HTTP request there too, on top of
tests/unit/controllers/test_mock.py exercising it directly in-process.
"""

from typing import Any

import jwt
from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.config import get_settings
from app.rate_limit import limiter

router = APIRouter(tags=["mock"])
settings = get_settings()

# At least 32 bytes so PyJWT doesn't warn about a weak HMAC key -- length matters
# here (PyJWT checks it), secrecy doesn't (see the module docstring above).
_MOCK_SIGNING_KEY = "mock-mode-signing-key-not-a-real-secret"


class MockTokenRequest(BaseModel):
    """Desired claims for a mock access token."""

    sub: str
    roles: list[str] = []


@router.post("/token")
@limiter.limit(settings.rate_limit_mock_token)
async def issue_mock_token(payload: MockTokenRequest, request: Request) -> dict[str, Any]:
    """Issue a mock access token carrying the given subject and client roles.

    `request: Request` is otherwise-unused here -- app.rate_limit's limiter.limit(...)
    decorator requires a parameter literally named `request` (or `websocket`) typed
    as a real Request to find the calling client's address, which is why the body
    param above is named `payload` instead of the more usual `request`.
    """
    claims = {
        "sub": payload.sub,
        "preferred_username": payload.sub,
        "resource_access": {settings.oidc_client_id: {"roles": payload.roles}},
    }
    return {"access_token": jwt.encode(claims, _MOCK_SIGNING_KEY, algorithm="HS256")}
