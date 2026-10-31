"""Unit test: decode_bearer_token rejects a malformed token, and require_roles gating."""

import json
import logging
from collections.abc import Callable
from functools import lru_cache
from typing import Annotated, Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from jwt import PyJWKClient, PyJWKSet

from app import oidc
from app.oidc import decode_bearer_token, get_current_claims, require_roles


def test_decode_bearer_token_rejects_a_malformed_token() -> None:
    """A token that isn't valid JWT structure is rejected with 401, no network needed."""
    with pytest.raises(HTTPException) as exc_info:
        decode_bearer_token("not-a-jwt")
    assert exc_info.value.status_code == 401


def test_decode_bearer_token_mock_mode_skips_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    """MODE=mock trusts the token's claims without checking its signature."""
    monkeypatch.setattr(oidc.settings, "mode", "mock")
    token = jwt.encode(
        {"sub": "test-user"}, "at-least-32-bytes-long-hmac-signing-key", algorithm="HS256"
    )
    assert decode_bearer_token(token)["sub"] == "test-user"


def test_get_current_claims_logs_a_warning_on_rejection(caplog: pytest.LogCaptureFixture) -> None:
    """A rejected bearer token is logged with the request path, for brute-force detection."""
    app = FastAPI()

    @app.get("/whoami")
    def whoami(claims: Annotated[dict[str, Any], Depends(get_current_claims)]) -> dict[str, Any]:
        return claims

    with caplog.at_level(logging.WARNING, logger="app.oidc"):
        response = TestClient(app).get("/whoami", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401
    assert "Rejected bearer token for /whoami" in caplog.text


def test_get_jwks_client_reports_503_on_discovery_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An unreachable/malformed OIDC discovery document is a 503, not an unhandled 500,
    and still goes through get_current_claims's own rejection log.
    """
    oidc._get_jwks_client.cache_clear()

    def _raise(url: str, *, timeout: float) -> httpx.Response:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "get", _raise)
    monkeypatch.setattr(oidc.settings, "mode", "dev")

    app = FastAPI()

    @app.get("/whoami")
    def whoami(claims: Annotated[dict[str, Any], Depends(get_current_claims)]) -> dict[str, Any]:
        return claims

    with caplog.at_level(logging.WARNING, logger="app.oidc"):
        response = TestClient(app).get("/whoami", headers={"Authorization": "Bearer x.y.z"})

    assert response.status_code == 503
    assert "Rejected bearer token for /whoami (status=503)" in caplog.text
    oidc._get_jwks_client.cache_clear()


def _require_roles_app() -> FastAPI:
    """A throwaway app with one route gated by require_roles("editor")."""
    app = FastAPI()

    @app.get("/gated")
    def gated(
        claims: Annotated[dict[str, Any], Depends(require_roles("editor"))],
    ) -> dict[str, Any]:
        return claims

    return app


def test_require_roles_allows_a_granted_role() -> None:
    """require_roles passes through when the caller has one of the required roles."""
    app = _require_roles_app()
    app.dependency_overrides[oidc.get_current_claims] = lambda: {
        "sub": "u",
        "resource_access": {oidc.settings.oidc_client_id: {"roles": ["editor"]}},
    }
    response = TestClient(app).get("/gated")
    assert response.status_code == 200


def test_require_roles_rejects_a_missing_role() -> None:
    """require_roles rejects with 403 when the caller lacks every required role."""
    app = _require_roles_app()
    app.dependency_overrides[oidc.get_current_claims] = lambda: {
        "sub": "u",
        "resource_access": {oidc.settings.oidc_client_id: {"roles": ["viewer"]}},
    }
    response = TestClient(app).get("/gated")
    assert response.status_code == 403


def test_require_roles_logs_a_warning_on_denial(caplog: pytest.LogCaptureFixture) -> None:
    """A 403 role denial is logged with the request path and subject, for auditing."""
    app = _require_roles_app()
    app.dependency_overrides[oidc.get_current_claims] = lambda: {
        "sub": "u",
        "resource_access": {oidc.settings.oidc_client_id: {"roles": ["viewer"]}},
    }
    with caplog.at_level(logging.WARNING, logger="app.oidc"):
        response = TestClient(app).get("/gated")
    assert response.status_code == 403
    assert "Denied role check for /gated" in caplog.text
    assert "'u'" in caplog.text


def test_require_roles_rejects_claims_with_no_resource_access() -> None:
    """require_roles rejects with 403 when the token carries no resource_access at all."""
    app = _require_roles_app()
    app.dependency_overrides[oidc.get_current_claims] = lambda: {"sub": "u"}
    response = TestClient(app).get("/gated")
    assert response.status_code == 403


_HS_KEY = "at-least-32-bytes-long-hmac-signing-key"


def _rsa_jwks_client(kid: str = "k1") -> tuple[PyJWKClient, rsa.RSAPrivateKey]:
    """A PyJWKClient whose JWK-set cache is pre-filled (no network), plus the private key."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    client = PyJWKClient("https://jwks.test.invalid/certs", cache_keys=True)
    assert client.jwk_set_cache is not None
    client.jwk_set_cache.put(PyJWKSet.from_dict({"keys": [jwk]}))
    return client, private


ISSUER = "http://localhost:8080/realms/test"
_FAR_FUTURE = 4_102_444_800  # 2100-01-01


def _claims_token(private: rsa.RSAPrivateKey, **claims: object) -> str:
    """A signed token with valid exp/iss/aud (api), overridable (None drops a claim)."""
    payload: dict[str, Any] = {"sub": "u", "iss": ISSUER, "aud": "api", "exp": _FAR_FUTURE}
    payload.update(claims)
    payload = {k: v for k, v in payload.items() if v is not None}
    return jwt.encode(payload, private, algorithm="RS256", headers={"kid": "k1"})


def _rs_token(private: rsa.RSAPrivateKey, kid: str) -> str:
    return jwt.encode({"sub": "u"}, private, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-mock mode, audience "api", issuer ISSUER, fresh JWKS-client cache."""
    monkeypatch.setattr(oidc.settings, "mode", "dev")
    monkeypatch.setattr(oidc.settings, "oidc_audience", "api")
    monkeypatch.setattr(oidc.settings, "oidc_issuer", ISSUER)
    monkeypatch.setattr(oidc.settings, "oidc_algorithm", "RS256")
    oidc._get_jwks_client.cache_clear()


def _install_client(monkeypatch: pytest.MonkeyPatch, client: PyJWKClient) -> None:
    """Make the lru-cached accessor return `client` (so cache_info() sees it as built)."""
    monkeypatch.setattr(oidc, "_get_jwks_client", lru_cache(lambda: client))
    oidc._get_jwks_client()


def _spy_threadpool(monkeypatch: pytest.MonkeyPatch) -> list[Callable[[str], dict[str, Any]]]:
    """Replace run_in_threadpool with an inline runner that records what was submitted."""
    calls: list[Callable[[str], dict[str, Any]]] = []

    async def _spy(func: Callable[..., Any], *args: str) -> object:
        calls.append(func)
        return func(*args)

    monkeypatch.setattr(oidc, "run_in_threadpool", _spy)
    return calls


async def test_warm_jwks_stays_on_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, dev_mode: None
) -> None:
    """A cached kid is verified without a threadpool hop."""
    client, private = _rsa_jwks_client()
    _install_client(monkeypatch, client)
    calls = _spy_threadpool(monkeypatch)
    token = _claims_token(private)
    claims = await oidc.decode_bearer_token_async(token)
    assert claims["sub"] == "u"
    assert calls == []


async def test_unknown_kid_runs_in_the_threadpool(
    monkeypatch: pytest.MonkeyPatch, dev_mode: None
) -> None:
    """A kid absent from the cached JWK set may trigger a refresh, so it goes to the pool."""
    client, private = _rsa_jwks_client()
    _install_client(monkeypatch, client)
    calls = _spy_threadpool(monkeypatch)
    monkeypatch.setattr(PyJWKClient, "fetch_data", lambda self: {"keys": []})
    with pytest.raises(HTTPException) as exc_info:
        await oidc.decode_bearer_token_async(_rs_token(private, "rotated"))
    assert exc_info.value.status_code == 401
    assert calls == [oidc.decode_bearer_token]


async def test_undiscovered_client_runs_in_the_threadpool(
    monkeypatch: pytest.MonkeyPatch, dev_mode: None
) -> None:
    """Before the JWKS client exists, discovery HTTP must not run on the loop."""
    calls = _spy_threadpool(monkeypatch)

    def _raise(url: str, *, timeout: float) -> httpx.Response:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "get", _raise)
    with pytest.raises(HTTPException) as exc_info:
        await oidc.decode_bearer_token_async("x.y.z")
    assert exc_info.value.status_code == 503
    assert calls == [oidc.decode_bearer_token]
    oidc._get_jwks_client.cache_clear()


async def test_expired_or_disabled_jwk_set_cache_runs_in_the_threadpool(
    monkeypatch: pytest.MonkeyPatch, dev_mode: None
) -> None:
    """An empty/expired (or disabled) JWK-set cache means a fetch is possible."""
    client, private = _rsa_jwks_client()
    assert client.jwk_set_cache is not None
    client.jwk_set_cache.put(None)
    _install_client(monkeypatch, client)
    assert oidc._jwks_is_warm(_rs_token(private, "k1")) is False
    client.jwk_set_cache = None
    assert oidc._jwks_is_warm(_rs_token(private, "k1")) is False


async def test_malformed_token_stays_on_the_loop(
    monkeypatch: pytest.MonkeyPatch, dev_mode: None
) -> None:
    """An unparseable token is rejected with 401 before any network call."""
    client, _ = _rsa_jwks_client()
    _install_client(monkeypatch, client)
    calls = _spy_threadpool(monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        await oidc.decode_bearer_token_async("not-a-jwt")
    assert exc_info.value.status_code == 401
    assert calls == []


async def test_mock_mode_async_stays_on_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """MODE=mock does no I/O, so no threadpool hop."""
    monkeypatch.setattr(oidc.settings, "mode", "mock")
    calls = _spy_threadpool(monkeypatch)
    token = jwt.encode({"sub": "m"}, _HS_KEY, algorithm="HS256")
    assert (await oidc.decode_bearer_token_async(token))["sub"] == "m"
    assert calls == []


async def test_prefetch_jwks_warms_the_cache(
    monkeypatch: pytest.MonkeyPatch, dev_mode: None
) -> None:
    """prefetch_jwks builds the client and fetches the JWK set via the threadpool."""
    fetched: list[bool] = []

    class _Client:
        def get_jwk_set(self) -> None:
            fetched.append(True)

    monkeypatch.setattr(oidc, "_get_jwks_client", _Client)
    calls = _spy_threadpool(monkeypatch)
    await oidc.prefetch_jwks()
    assert fetched == [True]
    assert len(calls) == 1


@pytest.mark.parametrize(
    "error",
    [
        HTTPException(503, "Authentication service unavailable"),
        jwt.PyJWKClientConnectionError("down"),
    ],
)
async def test_prefetch_jwks_failure_only_warns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    dev_mode: None,
    error: Exception,
) -> None:
    """An unreachable IdP must not fail startup."""

    def _boom() -> PyJWKClient:
        raise error

    monkeypatch.setattr(oidc, "_get_jwks_client", _boom)
    with caplog.at_level(logging.WARNING, logger="app.oidc"):
        await oidc.prefetch_jwks()
    assert "JWKS prefetch failed" in caplog.text


async def test_prefetch_jwks_is_a_noop_in_mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """MODE=mock has no IdP, so nothing is fetched."""
    monkeypatch.setattr(oidc.settings, "mode", "mock")

    def _boom() -> PyJWKClient:
        raise AssertionError("must not be called")

    monkeypatch.setattr(oidc, "_get_jwks_client", _boom)
    await oidc.prefetch_jwks()


@pytest.fixture
def signed(monkeypatch: pytest.MonkeyPatch, dev_mode: None) -> rsa.RSAPrivateKey:
    """dev_mode plus a warm JWKS client for key "k1"; returns the matching private key."""
    client, private = _rsa_jwks_client()
    _install_client(monkeypatch, client)
    return private


def test_valid_token_is_accepted(signed: rsa.RSAPrivateKey) -> None:
    """A token with the expected issuer and audience passes."""
    assert decode_bearer_token(_claims_token(signed))["aud"] == "api"


def test_audience_list_containing_the_expected_value_is_accepted(
    signed: rsa.RSAPrivateKey,
) -> None:
    """Keycloak emits `aud` as a list when several mappers apply; one match is enough."""
    assert decode_bearer_token(_claims_token(signed, aud=["account", "api"]))["sub"] == "u"


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "someone-else"},
        {"aud": None},
        {"iss": "http://keycloak:8080/realms/test"},
        {"iss": None},
        {"exp": None},
    ],
)
def test_wrong_or_missing_aud_iss_exp_is_rejected(
    signed: rsa.RSAPrivateKey, claims: dict[str, Any]
) -> None:
    """The aud check cannot be skipped; iss must equal the configured issuer exactly."""
    with pytest.raises(HTTPException) as exc_info:
        decode_bearer_token(_claims_token(signed, **claims))
    assert exc_info.value.status_code == 401
    assert exc_info.value.headers is not None
    assert 'error="invalid_token"' in exc_info.value.headers["WWW-Authenticate"]


def test_issuer_is_independent_of_the_discovery_url(
    monkeypatch: pytest.MonkeyPatch, dev_mode: None
) -> None:
    """Discovery is fetched from discovery_url while tokens are checked against oidc_issuer."""
    oidc._get_jwks_client.cache_clear()
    monkeypatch.setattr(
        oidc.settings, "oidc_discovery_url", "http://keycloak:8080/realms/test/.well-known/x"
    )
    fetched: list[str] = []

    def _get(url: str, *, timeout: float) -> httpx.Response:
        fetched.append(url)
        return httpx.Response(
            200, json={"jwks_uri": "http://keycloak:8080/certs"}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx, "get", _get)
    oidc._get_jwks_client()
    assert fetched == ["http://keycloak:8080/realms/test/.well-known/x"]
    oidc._get_jwks_client.cache_clear()


def test_missing_token_challenge_points_at_the_metadata_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No credentials: 401 with a bare Bearer challenge carrying resource_metadata."""
    monkeypatch.setattr(oidc.settings, "public_base_url", "https://api.example.com")
    app = FastAPI()

    @app.get("/whoami")
    def whoami(claims: Annotated[dict[str, Any], Depends(get_current_claims)]) -> dict[str, Any]:
        return claims

    response = TestClient(app).get("/whoami", headers={"Host": "evil.example"})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == (
        'Bearer resource_metadata="https://api.example.com/.well-known/oauth-protected-resource"'
    )


def test_bearer_challenge_adds_the_error_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """An error code is appended after resource_metadata."""
    monkeypatch.setattr(oidc.settings, "public_base_url", "https://api.example.com")
    assert oidc.bearer_challenge("invalid_token")["WWW-Authenticate"].endswith(
        'oauth-protected-resource", error="invalid_token"'
    )
