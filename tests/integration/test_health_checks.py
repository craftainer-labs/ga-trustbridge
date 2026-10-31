"""Integration test: each concrete health check against its real stack service.

Also covers /health/ready end-to-end, via app.main's actually-wired registry.
"""

import boto3
from craftainer_observability.health.checks.http import HTTPCheck
from craftainer_observability.health.checks.postgres import PostgresCheck
from craftainer_observability.health.checks.redis import RedisCheck
from craftainer_observability.health.checks.s3 import S3Check
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import engine
from app.main import app


async def test_database_check_against_real_postgres() -> None:
    """PostgresCheck reports healthy against the live postgres stack service."""
    result = await PostgresCheck(engine).check()
    assert result.healthy is True


async def test_redis_check_against_real_redis() -> None:
    """RedisCheck reports healthy against the live redis stack service."""
    check = RedisCheck(get_settings().redis_url)
    try:
        result = await check.check()
    finally:
        await check.aclose()
    assert result.healthy is True


async def test_s3_check_against_real_s3() -> None:
    """S3Check reports healthy against the live s3 (RustFS) stack service."""
    settings = get_settings()
    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
    )
    result = await S3Check(client).check()
    assert result.healthy is True


async def test_oidc_check_against_real_keycloak() -> None:
    """HTTPCheck reports healthy against the live keycloak stack service."""
    result = await HTTPCheck(get_settings().discovery_url).check()
    assert result.healthy is True


def test_ready_reports_every_stack_service_healthy() -> None:
    """GET /health/ready returns 200 with every real external service healthy."""
    # `with`: the lifespan's shutdown closes the wired RedisCheck's client on the same loop.
    with TestClient(app) as ready_client:
        response = ready_client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert set(body["checks"]) == {"database", "redis", "s3", "oidc"}
    assert all(check["healthy"] for check in body["checks"].values())
