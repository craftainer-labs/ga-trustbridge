"""Fixtures for the GitHub core: hosts, a key-backed signer, and a scripted GitHub."""

import base64
import json
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import httpx2
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from nacl.public import PrivateKey

from trustbridge.adapters.interface import GitHubHost, Tenant
from trustbridge.github.availability import HostMonitor, MonitorDeps
from trustbridge.github.client import GitHubApi, GitHubRequester
from trustbridge.github.gateway import GatewayDeps, TenantGateway
from trustbridge.github.hosts import GitHubHostConfig
from trustbridge.github.tokens import InstallationTokenProvider


class KeySigner:
    """An in-memory signer over a real RSA key (the stand-in for any backend)."""

    def __init__(
        self, key: rsa.RSAPrivateKey | None = None, advertised: rsa.RSAPrivateKey | None = None
    ) -> None:
        """`advertised` makes `public_key_pem` report a different key than the one signing."""
        self.key = key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.advertised = advertised or self.key
        self.fail = False
        self.key_id = "test-key"

    async def sign_jwt(self, signing_input: bytes) -> bytes:
        """Sign RS256 (or fail, to simulate a backend outage)."""
        if self.fail:
            msg = "backend down"
            raise RuntimeError(msg)
        return self.key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())

    async def public_key_pem(self) -> str:
        """Return the advertised public key."""
        return (
            self.advertised.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode()
        )


def make_host_config(
    name: str = "github.com", *, app_id: int | None = 77, kind: str = "github.com"
) -> GitHubHostConfig:
    """A github.com-shaped host config (registered unless `app_id` is None)."""
    return GitHubHostConfig.model_validate(
        {
            "name": name,
            "kind": kind,
            "web_url": "https://github.com",
            "api_url": "https://api.github.com",
            "oidc_issuer": "https://token.actions.githubusercontent.com",
            "registry_host": "ghcr.io",
            "oidc_id_claims": True,
            "signer": "main",
            "app_id": app_id,
            "webhook_secret_ref": "ref/webhook" if app_id is not None else None,
        }
    )


def make_tenant(host: GitHubHostConfig) -> Tenant:
    """A tenant on `host`."""
    projected: GitHubHost = host.to_host()
    return Tenant(id=uuid4(), github_host=projected, org="acme")


class FakeChecker:
    """`HostChecker` with scripted answers and call counters."""

    def __init__(
        self, app_id: int = 77, issuer: str = "https://token.actions.githubusercontent.com"
    ) -> None:
        """Answer `GET /app` with `app_id` and discovery with `issuer`."""
        self.app_id = app_id
        self.issuer = issuer
        self.app_calls = 0
        self.discovery_calls = 0
        self.last_jwt: str | None = None

    async def get_app_id(self, host: GitHubHostConfig, jwt: str) -> int:
        """Return the scripted App id."""
        del host
        self.app_calls += 1
        self.last_jwt = jwt
        return self.app_id

    async def discovery_issuer(self, host: GitHubHostConfig) -> str:
        """Return the scripted issuer."""
        del host
        self.discovery_calls += 1
        return self.issuer


class ScriptedGitHub:
    """A scripted GitHub REST API behind an `httpx2.MockTransport`.

    Routes are `(METHOD, path)` to a callable `(request) -> httpx2.Response`; unmatched requests
    raise, so a test fails on any call it did not expect. Every request is recorded.
    """

    def __init__(self) -> None:
        """Start with no routes."""
        self.routes: dict[tuple[str, str], Callable[[httpx2.Request], httpx2.Response]] = {}
        self.requests: list[httpx2.Request] = []
        self.sodium = PrivateKey.generate()

    def route(self, method: str, path: str, status: int = 200, body: object = None) -> None:
        """Answer `method path` with a fixed JSON response."""
        self.routes[(method, path)] = lambda _r: httpx2.Response(status, json=body)

    def on(self, method: str, path: str, fn: Callable[[httpx2.Request], httpx2.Response]) -> None:
        """Answer `method path` with a function."""
        self.routes[(method, path)] = fn

    def public_key_body(self) -> dict[str, str]:
        """The `{key_id, key}` body GitHub returns for a secrets public key."""
        return {"key_id": "kid1", "key": base64.b64encode(bytes(self.sodium.public_key)).decode()}

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        """The transport handler."""
        self.requests.append(request)
        fn = self.routes.get((request.method, request.url.path))
        if fn is None:
            msg = f"unexpected request {request.method} {request.url.path}"
            raise AssertionError(msg)
        return fn(request)

    def transport(self) -> httpx2.MockTransport:
        """The mock transport to hand to `GitHubApi`."""
        return httpx2.MockTransport(self.handler)

    def paths(self) -> list[str]:
        """`METHOD path` of every request, in order."""
        return [f"{r.method} {r.url.path}" for r in self.requests]

    def json_of(self, method: str, path: str) -> dict[str, Any]:
        """The JSON body of the last request to `method path`."""
        for r in reversed(self.requests):
            if (r.method, r.url.path) == (method, path):
                return dict(json.loads(r.content))
        msg = f"no {method} {path}"
        raise AssertionError(msg)


class FixedInstallation:
    """`InstallationResolver` returning one installation id."""

    def __init__(self, installation_id: int) -> None:
        """Hold the id."""
        self.id = installation_id

    async def installation_id(self, tenant_id: UUID) -> int:
        """Return the id for any tenant."""
        del tenant_id
        return self.id


class Env:
    """A ready host, signer, scripted GitHub and the gateway on top of them."""

    def __init__(self, gh: ScriptedGitHub) -> None:
        """Build the wiring; call `await env.start()` before use."""
        self.gh = gh
        self.host = make_host_config()
        self.signer = KeySigner()
        self.checker = FakeChecker()
        self.monitor = HostMonitor(
            [self.host], {"main": self.signer}, MonitorDeps(checker=self.checker)
        )
        self.clock_now = 1_800_000_000.0

        def factory(host: GitHubHostConfig) -> GitHubRequester:
            return GitHubApi(host, async_transport=gh.transport())

        self.tokens = InstallationTokenProvider(
            self.monitor, {"main": self.signer}, factory, clock=lambda: self.clock_now
        )
        self.installation_id = 4242

        self.gateway = TenantGateway(
            GatewayDeps(self.monitor, self.tokens, FixedInstallation(self.installation_id), factory)
        )
        self.tenant = make_tenant(self.host)
        self.mint_count = 0

    async def start(self) -> None:
        """Mark the host ready and script the token endpoint."""
        await self.monitor.check_all()

        def mint(request: httpx2.Request) -> httpx2.Response:
            self.mint_count += 1
            assert request.headers["Authorization"].startswith("Bearer ")
            return httpx2.Response(
                201, json={"token": f"ghs_{self.mint_count}", "expires_at": "2027-01-15T09:00:00Z"}
            )

        self.gh.on("POST", f"/app/installations/{self.installation_id}/access_tokens", mint)


@pytest.fixture
def gh() -> ScriptedGitHub:
    """A scripted GitHub with no routes."""
    return ScriptedGitHub()


@pytest.fixture
async def env(gh: ScriptedGitHub) -> Env:
    """A started environment (host ready, token endpoint scripted)."""
    e = Env(gh)
    await e.start()
    # 2027-01-15T09:00:00Z minus 30 minutes: tokens are fresh for the whole test.
    e.clock_now = 1_800_000_000.0
    return e
