"""Benchmark: app.oidc.decode_bearer_token verifying an RS256 token from a pre-loaded JWKS."""

from pytest_benchmark.fixture import BenchmarkFixture

from app.oidc import decode_bearer_token


def test_decode_bearer_token_rs256(benchmark: BenchmarkFixture, bearer_token: str) -> None:
    """Signature + exp/iss/aud validation, with no network (the key is served from cache)."""
    claims = benchmark(decode_bearer_token, bearer_token)
    assert claims["sub"] == "bench-user"
