"""Shared fixtures for tests/bench: an RS256 signing key and a MODE=mock in-process app."""

import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
import jwt
import pytest
from craftainer_crud import config as crud_config
from craftainer_crud.config import CrudConfig
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWKClient, PyJWKSet

from app import oidc
from app.config import get_settings
from app.main import app
from app.oidc import get_current_claims

BENCH_KID = "bench-key"
BENCH_ISSUER = "https://issuer.bench.invalid/realms/bench"
BENCH_AUDIENCE = "bench-api"
SEEDED_HEROES = 25


@pytest.fixture(scope="session")
def signing_key() -> rsa.RSAPrivateKey:
    """One RS256 private key for the whole bench run (generation is slow, not benchmarked)."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def bearer_token(signing_key: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch) -> str:
    """A signed token, with app.oidc pointed at a JWKS client whose cache is pre-filled.

    The PyJWKClient's `jwk_set_cache` is primed with the key's JWK, so
    `get_signing_key_from_jwt` is answered from memory and never fetches (the URI is
    unresolvable on purpose, so an accidental fetch would fail rather than slow a run).
    """
    jwk: dict[str, Any] = jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)
    jwk.update({"kid": BENCH_KID, "use": "sig", "alg": "RS256"})
    client = PyJWKClient("https://jwks.bench.invalid/certs", cache_keys=True)
    assert client.jwk_set_cache is not None
    client.jwk_set_cache.put(PyJWKSet.from_dict({"keys": [jwk]}))

    monkeypatch.setattr(oidc, "_get_jwks_client", lambda: client)
    monkeypatch.setattr(oidc.settings, "mode", "dev")
    monkeypatch.setattr(oidc.settings, "oidc_algorithm", "RS256")
    monkeypatch.setattr(oidc.settings, "oidc_audience", BENCH_AUDIENCE)
    monkeypatch.setattr(oidc.settings, "oidc_issuer_url", BENCH_ISSUER)
    # OIDC_ISSUER (set by keycloak.env in the devcontainer) outranks OIDC_ISSUER_URL.
    monkeypatch.setattr(oidc.settings, "oidc_issuer", BENCH_ISSUER)
    return jwt.encode(
        {
            "sub": "bench-user",
            "iss": BENCH_ISSUER,
            "aud": BENCH_AUDIENCE,
            "exp": 4_102_444_800,  # 2100-01-01
        },
        signing_key,
        algorithm="RS256",
        headers={"kid": BENCH_KID},
    )


@pytest.fixture(scope="session")
def event_loop_runner() -> Iterator[asyncio.Runner]:
    """One event loop reused across benchmark rounds (benchmarked callables are sync)."""
    with asyncio.Runner() as runner:
        yield runner


@pytest.fixture
def mock_app_client(event_loop_runner: asyncio.Runner) -> Iterator[httpx.AsyncClient]:
    """An in-process ASGI client against the real app, wired as under MODE=mock.

    The app module is imported once under whatever MODE the process started with, so
    this fixture applies the two things MODE=mock changes for a request: the crud
    package's in-memory repository (`CrudConfig.use_memory`) and settings.mode. Auth is
    overridden with fixed claims (the token path has its own benchmark), and no
    lifespan runs (MODE=mock skips migrations anyway). Nothing here touches Postgres,
    Redis, S3 or Keycloak.
    """
    settings = get_settings()
    previous_mode = settings.mode
    previous_config = crud_config.get_config()
    previous_factory = crud_config._session_factory
    settings.mode = "mock"
    crud_config.configure(
        CrudConfig(
            use_memory=True, bulk_action_max_matched=previous_config.bulk_action_max_matched
        ),
        previous_factory,
    )
    app.dependency_overrides[get_current_claims] = lambda: {
        "sub": "bench-user",
        "resource_access": {settings.oidc_client_id: {"roles": ["viewer", "editor"]}},
    }
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bench")

    async def _seed() -> None:
        for index in range(SEEDED_HEROES):
            response = await client.post(
                "/crud/v1/heroes/v2/json", json={"name": f"Hero {index}", "powers": ["Flight"]}
            )
            assert response.status_code == 201, response.text

    event_loop_runner.run(_seed())
    yield client
    event_loop_runner.run(client.aclose())
    del app.dependency_overrides[get_current_claims]
    crud_config.configure(previous_config, previous_factory)
    settings.mode = previous_mode
