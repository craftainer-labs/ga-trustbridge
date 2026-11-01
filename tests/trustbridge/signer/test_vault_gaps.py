"""Vault Transit signer edge paths: AppRole login, failures, client lifecycle, key checks."""

import json
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from pydantic import SecretStr

from tests.trustbridge.github.test_gaps import _ca_bundle
from tests.trustbridge.signer.test_signers import FakeVault, make_key, pem_of, vault_signer
from trustbridge.signer import SignerError
from trustbridge.signer.config import VaultTransitSignerConfig
from trustbridge.signer.file import FileSigner
from trustbridge.signer.vault import VaultTransitSigner


def _config(tmp_path: Path, **extra: object) -> VaultTransitSignerConfig:
    secret_id = tmp_path / "secret-id"
    secret_id.write_text("sid\n")
    return VaultTransitSignerConfig.model_validate(
        {
            "kind": "vault-transit",
            "addr": "https://vault.example",
            "key": "app",
            "auth": {
                "method": "approle",
                "role_id": "rid",
                "secret_id_file": str(secret_id),
            },
            **extra,
        }
    )


def _client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler, base_url="https://vault.example")


async def test_approle_login_sends_the_namespace_and_the_secret_id_from_the_file(
    tmp_path: Path,
) -> None:
    """AppRole auth posts role id and secret id; the configured namespace rides on every call."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/auth/approle/login":
            assert json.loads(request.content) == {"role_id": "rid", "secret_id": "sid"}
            return httpx.Response(200, json={"auth": {"client_token": "t", "lease_duration": 60}})
        return httpx.Response(200, json={"data": {"signature": "vault:v1:" + "QQ=="}})

    signer = VaultTransitSigner(
        _config(tmp_path, namespace="team"), _client(httpx.MockTransport(handler))
    )
    assert await signer.sign_jwt(b"x") == b"A"
    assert {r.headers["X-Vault-Namespace"] for r in seen} == {"team"}


async def test_failed_login_and_failed_calls_are_signer_errors(tmp_path: Path) -> None:
    """Every failing Vault answer is a `SignerError` naming the status, never a leak."""
    status = {"login": 500}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/login"):
            if status["login"] != 200:
                return httpx.Response(status["login"])
            return httpx.Response(200, json={"auth": {"client_token": "t", "lease_duration": 60}})
        return httpx.Response(500)

    signer = VaultTransitSigner(_config(tmp_path), _client(httpx.MockTransport(handler)))
    with pytest.raises(SignerError, match="login failed with HTTP 500"):
        await signer.sign_jwt(b"x")
    status["login"] = 200
    with pytest.raises(SignerError, match="sign failed with HTTP 500"):
        await signer.sign_jwt(b"x")
    with pytest.raises(SignerError, match="key read failed with HTTP 500"):
        await signer.public_key_pem()
    with pytest.raises(SignerError, match="wrapping key read failed with HTTP 500"):
        await signer.import_key(SecretStr(pem_of(make_key())))


async def test_a_transport_failure_is_a_signer_error(tmp_path: Path) -> None:
    """Connection errors do not escape as raw httpx errors."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"auth": {"client_token": "t", "lease_duration": 60}})
        raise httpx.ConnectError("refused", request=request)

    signer = VaultTransitSigner(_config(tmp_path), _client(httpx.MockTransport(handler)))
    with pytest.raises(SignerError, match="ConnectError"):
        await signer.sign_jwt(b"x")


async def test_import_rejects_non_rsa_keys_and_non_rsa_wrapping_keys(tmp_path: Path) -> None:
    """Both the App key and Vault's wrapping key must be RSA."""
    vault = FakeVault(make_key(), tmp_path)
    signer = vault_signer(tmp_path, vault)
    ec_pem = (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    with pytest.raises(SignerError, match="must be an RSA key"):
        await signer.import_key(SecretStr(ec_pem))

    def ec_wrapping(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"auth": {"client_token": "t", "lease_duration": 60}})
        public = (
            ec.generate_private_key(ec.SECP256R1())
            .public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode()
        )
        return httpx.Response(200, json={"data": {"public_key": public}})

    odd = VaultTransitSigner(_config(tmp_path), _client(httpx.MockTransport(ec_wrapping)))
    with pytest.raises(SignerError, match="not RSA"):
        await odd.import_key(SecretStr(pem_of(make_key())))


async def test_a_failed_import_is_a_signer_error(tmp_path: Path) -> None:
    """Vault refusing the wrapped key is reported with its status."""
    vault = FakeVault(make_key(), tmp_path)
    signer = vault_signer(tmp_path, vault)
    real = vault.handler

    def failing_import(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/import_version"):
            return httpx.Response(400)
        return real(request)

    signer._client = _client(httpx.MockTransport(failing_import))
    with pytest.raises(SignerError, match="import failed with HTTP 400"):
        await signer.import_key(SecretStr(pem_of(make_key())))


async def test_the_client_is_created_lazily_with_the_ca_bundle_and_closed(tmp_path: Path) -> None:
    """Without an injected client the signer builds one; `aclose` closes only what exists."""
    bare = VaultTransitSigner(_config(tmp_path))
    await bare.aclose()  # nothing created yet
    assert bare._http() is bare._http()
    await bare.aclose()
    with_ca = VaultTransitSigner(_config(tmp_path, ca_bundle=str(_ca_bundle(tmp_path))))
    with_ca._http()
    await with_ca.aclose()


async def test_the_file_signer_refuses_small_or_non_rsa_keys(tmp_path: Path) -> None:
    """Keys under 2048 bits and non-RSA keys are refused."""
    small = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    path = tmp_path / "small.pem"
    path.write_text(pem_of(small))
    path.chmod(0o600)
    with pytest.raises(SignerError, match="at least"):
        await FileSigner(path).sign_jwt(b"x")
