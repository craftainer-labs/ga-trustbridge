"""GitHub App JWT assembly through a signer (ADR 0030), tested for its limits.

githubkit's `AppAuthStrategy` signs in-process and needs the PEM, so trustbridge builds
`header.payload`, asks the signer for the RS256 signature and assembles the token itself.
"""

import base64
import json
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from trustbridge.signer.base import AppKeySigner

MAX_LIFETIME_SECONDS = 9 * 60
"""`exp` is at most 9 minutes ahead (GitHub's hard limit is 10)."""

CLOCK_SKEW_SECONDS = 60
"""`iat` is this far in the past, absorbing clock drift between us and GitHub."""

SELFTEST_PROBE = b"trustbridge-signer-selftest"


def b64url(data: bytes) -> str:
    """Return unpadded base64url."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwt_claims(
    issuer: int | str, now: float, *, lifetime: int = MAX_LIFETIME_SECONDS
) -> dict[str, int | str]:
    """Return the claims; `iat` never lies after `now` and `exp` is at most 9 minutes ahead."""
    bounded = max(1, min(lifetime, MAX_LIFETIME_SECONDS))
    issued = int(now) - CLOCK_SKEW_SECONDS
    return {"iat": issued, "exp": int(now) + bounded, "iss": issuer}


async def build_app_jwt(
    signer: AppKeySigner,
    issuer: int | str,
    *,
    now: float | None = None,
) -> str:
    """Build and sign an App JWT (`iss` is the App id or client id)."""
    current = time.time() if now is None else now
    header = {"alg": "RS256", "typ": "JWT"}
    signing_input = (
        b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + b64url(json.dumps(jwt_claims(issuer, current), separators=(",", ":")).encode())
    )
    signature = await signer.sign_jwt(signing_input.encode())
    return f"{signing_input}.{b64url(signature)}"


def verify_rs256(public_key_pem: str, signing_input: bytes, signature: bytes) -> bool:
    """Return True when `signature` is a valid RS256 signature of `signing_input`."""
    key = serialization.load_pem_public_key(public_key_pem.encode())
    if not isinstance(key, rsa.RSAPublicKey):
        return False
    try:
        key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        return False
    return True
