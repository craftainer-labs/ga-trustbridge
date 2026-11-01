"""Builders shared by the trustbridge tests."""

import hashlib
import hmac
from typing import Any, cast

from redis.asyncio import Redis

from tests.trustbridge.fakes import FakeHostChecker, FakeRedis
from tests.trustbridge.github.conftest import KeySigner
from trustbridge.github.availability import HostMonitor, MonitorDeps
from trustbridge.github.hosts import GitHubHostConfig, load_hosts
from trustbridge.settings import TrustbridgeSettings

SIGNERS: dict[str, Any] = {"dev": {"kind": "file", "path": "/run/secrets/app.pem"}}


def host_dict(name: str = "github.com", **overrides: Any) -> dict[str, Any]:  # noqa: ANN401
    """The raw entry of a registered github.com-shaped host (override fields as needed)."""
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
    return data


def host(name: str = "github.com", **overrides: Any) -> GitHubHostConfig:  # noqa: ANN401
    """A parsed host (the loader applies the kind-dependent `oidc_id_claims` default)."""
    return load_hosts([host_dict(name, **overrides)])[0]


def make_settings(
    hosts: list[Any] | None = None, **overrides: Any  # noqa: ANN401
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


def make_monitor(
    hosts: list[GitHubHostConfig],
    checker: FakeHostChecker | None = None,
    **deps: Any,  # noqa: ANN401
) -> HostMonitor:
    """A real `HostMonitor` whose signer is an in-memory RSA key and whose network is scripted."""
    return HostMonitor(
        hosts, {"dev": KeySigner()}, MonitorDeps(checker=checker or FakeHostChecker(), **deps)
    )


def sign(secret: bytes, body: bytes) -> str:
    """The `X-Hub-Signature-256` value for `body`."""
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def as_redis(fake: FakeRedis) -> Redis:
    """The fake, typed as the client the code expects."""
    return cast(Redis, fake)
