"""Builders shared by the trustbridge tests."""

import hashlib
import hmac
from typing import Any, cast

from redis.asyncio import Redis

from tests.trustbridge.fakes import FakeRedis
from trustbridge.settings import GitHubHostConfig, TrustbridgeSettings

SIGNERS: dict[str, Any] = {"dev": {"kind": "file", "path": "/run/secrets/app.pem"}}


def host(name: str = "github.com", **overrides: Any) -> GitHubHostConfig:  # noqa: ANN401
    """A registered github.com-shaped host (override fields as needed)."""
    data: dict[str, Any] = {
        "name": name,
        "kind": "github.com",
        "web_url": "https://github.com",
        "api_url": "https://api.github.com",
        "oidc_issuer": "https://token.actions.githubusercontent.com",
        "registry_host": "ghcr.io",
        "app_id": 123,
        "signer": "dev",
        "webhook_secret_ref": f"env:SECRET_{name.upper().replace('.', '_')}",
    }
    data.update(overrides)
    return GitHubHostConfig.model_validate(data)


def make_settings(
    hosts: list[GitHubHostConfig] | None = None, **overrides: Any  # noqa: ANN401
) -> TrustbridgeSettings:
    """Mock-mode settings for unit tests (no database, no Valkey required)."""
    values: dict[str, Any] = {
        "github_hosts": hosts if hosts is not None else [host()],
        "signers": SIGNERS,
        "mode": "mock",
        "allow_mock_mode": True,
    }
    values.update(overrides)
    return TrustbridgeSettings(**values)


def sign(secret: bytes, body: bytes) -> str:
    """The `X-Hub-Signature-256` value for `body`."""
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def as_redis(fake: FakeRedis) -> Redis:
    """The fake, typed as the client the code expects."""
    return cast(Redis, fake)
