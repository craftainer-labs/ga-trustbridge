"""The `file` and `vault-transit` signers, and the `SIGNERS` configuration."""

import base64
import json
import os
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.keywrap import aes_key_unwrap_with_padding
from pydantic import SecretStr, ValidationError

from trustbridge.github.app_jwt import verify_rs256
from trustbridge.signer import SignerError, build_signers, parse_signers
from trustbridge.signer.base import AppKeySigner, KeyImporter
from trustbridge.signer.config import VaultTransitSignerConfig
from trustbridge.signer.file import FileSigner
from trustbridge.signer.vault import VaultTransitSigner


def make_key() -> rsa.RSAPrivateKey:
    """Generate a 2048-bit RSA key."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pem_of(key: rsa.RSAPrivateKey) -> str:
    """PKCS#8 PEM of a private key."""
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def public_pem_of(key: rsa.RSAPrivateKey) -> str:
    """SubjectPublicKeyInfo PEM of a key's public half."""
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


# ---- file ---------------------------------------------------------------------------------
async def test_file_signer_signs_rs256(tmp_path: Path) -> None:
    """The signature verifies against the public key the signer reports."""
    key = make_key()
    path = tmp_path / "app.pem"
    path.write_text(pem_of(key))
    path.chmod(0o600)
    signer = FileSigner(path)
    sig = await signer.sign_jwt(b"a.b")
    assert verify_rs256(await signer.public_key_pem(), b"a.b", sig)
    assert signer.key_id.startswith("sha256:")
    assert isinstance(signer, AppKeySigner)


async def test_file_signer_refuses_group_readable_keys(tmp_path: Path) -> None:
    """Permissions are checked: 0640 is refused."""
    path = tmp_path / "app.pem"
    path.write_text(pem_of(make_key()))
    path.chmod(0o640)
    with pytest.raises(SignerError, match="group or others"):
        await FileSigner(path).sign_jwt(b"x")


async def test_file_signer_reports_missing_and_invalid_files(tmp_path: Path) -> None:
    """A missing file (host not registered yet) or garbage is a SignerError, not a crash."""
    signer = FileSigner(tmp_path / "absent.pem")
    with pytest.raises(SignerError):
        await signer.sign_jwt(b"x")
    bad = tmp_path / "bad.pem"
    bad.write_text("not a key")
    bad.chmod(0o600)
    with pytest.raises(SignerError, match="PEM"):
        await FileSigner(bad).public_key_pem()


async def test_file_signer_import_writes_a_0600_file(tmp_path: Path) -> None:
    """The manifest flow's key lands with mode 0600 and is usable at once."""
    path = tmp_path / "new.pem"
    signer = FileSigner(path)
    key = make_key()
    await signer.import_key(SecretStr(pem_of(key)))
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert isinstance(signer, KeyImporter)
    assert await signer.public_key_pem() == public_pem_of(key)


async def test_file_signer_import_rejects_non_rsa_garbage(tmp_path: Path) -> None:
    """A PEM that is not an RSA private key is rejected before anything is written."""
    signer = FileSigner(tmp_path / "x.pem")
    with pytest.raises(SignerError):
        await signer.import_key(SecretStr("nope"))
    assert not (tmp_path / "x.pem").exists()


# ---- configuration --------------------------------------------------------------------------
def test_parse_signers_builds_discriminated_configs() -> None:
    """The ADR's example parses into `file` and `vault-transit` entries."""
    cfgs = parse_signers(
        json.dumps(
            {
                "dev": {"kind": "file", "path": "/run/secrets/app.pem"},
                "prod": {
                    "kind": "vault-transit",
                    "addr": "https://vault.example",
                    "mount": "transit",
                    "key": "trustbridge-ghes-prod",
                    "auth": {"method": "kubernetes", "role": "trustbridge"},
                },
            }
        )
    )
    assert set(cfgs) == {"dev", "prod"}
    signers = build_signers(cfgs)
    assert isinstance(signers["dev"], FileSigner)
    assert isinstance(signers["prod"], VaultTransitSigner)


@pytest.mark.parametrize(
    "entry",
    [
        {"kind": "file", "path": "/p", "pem": "x"},
        {"kind": "file", "path": "-----BEGIN " + "PRIVATE KEY-----"},
        {"kind": "aws-kms", "key": "k"},
        {
            "kind": "vault-transit",
            "addr": "https://v",
            "key": "k",
            "auth": {"method": "approle", "role_id": "r", "secret_id": "inline"},
        },
        {
            "kind": "vault-transit",
            "addr": "https://v",
            "key": "k",
            "auth": {"method": "token", "token": "hvs.abc"},
        },
        {
            "kind": "vault-transit",
            "addr": "http://v",
            "key": "k",
            "auth": {"method": "kubernetes", "role": "r"},
        },
    ],
)
def test_signer_configuration_never_holds_an_inline_secret(entry: dict[str, object]) -> None:
    """0030 invariant 5: credentials are file references; inline values cannot be expressed."""
    with pytest.raises(ValidationError):
        parse_signers({"s": entry})


# ---- vault transit --------------------------------------------------------------------------
class FakeVault:
    """A tiny Vault: Kubernetes login, Transit sign/key read, wrapping key and import."""

    def __init__(self, key: rsa.RSAPrivateKey, jwt_file: Path) -> None:
        """Hold the Transit key and the file the login JWT comes from."""
        self.key = key
        self.wrapping = make_key()
        self.requests: list[tuple[str, str]] = []
        self.logins = 0
        self.reject_next_token = False
        self.imported: dict[str, object] | None = None
        self.exists = True
        self.jwt_file = jwt_file

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Dispatch one request."""
        path = request.url.path
        self.requests.append((request.method, path))
        if path == "/v1/auth/kubernetes/login":
            return self._login(request)
        if self.reject_next_token:
            self.reject_next_token = False
            return httpx.Response(403, json={"errors": ["permission denied"]})
        if path == "/v1/transit/sign/app/sha2-256":
            return self._sign(request)
        if path.endswith(("/import", "/import_version")):
            self.imported = {"path": path, **json.loads(request.content)}
            return httpx.Response(204)
        return self._read(request)

    def _login(self, request: httpx.Request) -> httpx.Response:
        self.logins += 1
        assert json.loads(request.content) == {"role": "tb", "jwt": "sa-jwt"}
        return httpx.Response(
            200, json={"auth": {"client_token": f"tok{self.logins}", "lease_duration": 3600}}
        )

    def _sign(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["signature_algorithm"] == "pkcs1v15"
        assert body["prehashed"] is False
        data = base64.b64decode(body["input"])
        sig = self.key.sign(data, padding.PKCS1v15(), hashes.SHA256())
        return httpx.Response(
            200, json={"data": {"signature": "vault:v1:" + base64.b64encode(sig).decode()}}
        )

    def _read(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/transit/keys/app" and request.method == "GET":
            if not self.exists:
                return httpx.Response(404, json={"errors": []})
            keys = {"1": {"public_key": public_pem_of(self.key)}}
            return httpx.Response(200, json={"data": {"latest_version": 1, "keys": keys}})
        if path == "/v1/transit/wrapping_key":
            return httpx.Response(200, json={"data": {"public_key": public_pem_of(self.wrapping)}})
        return httpx.Response(404)


def vault_signer(tmp_path: Path, vault: FakeVault) -> VaultTransitSigner:
    """A signer talking to `vault` through a mock transport."""
    jwt_file = tmp_path / "token"
    jwt_file.write_text("sa-jwt\n")
    cfg = VaultTransitSignerConfig.model_validate(
        {
            "kind": "vault-transit",
            "addr": "https://vault.example",
            "key": "app",
            "auth": {"method": "kubernetes", "role": "tb", "jwt_file": str(jwt_file)},
        }
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(vault.handler), base_url="https://vault.example"
    )
    return VaultTransitSigner(cfg, client)


async def test_vault_signature_verifies_and_token_is_cached(tmp_path: Path) -> None:
    """Sign via Transit with pkcs1v15/prehashed=false; one login serves several calls."""
    vault = FakeVault(make_key(), tmp_path)
    signer = vault_signer(tmp_path, vault)
    sig = await signer.sign_jwt(b"h.p")
    await signer.sign_jwt(b"h.p")
    assert verify_rs256(await signer.public_key_pem(), b"h.p", sig)
    assert vault.logins == 1
    assert signer.key_id == "vault-transit:transit/app"


async def test_vault_relogs_in_once_after_a_403(tmp_path: Path) -> None:
    """An expired or revoked token (403) triggers one fresh login."""
    vault = FakeVault(make_key(), tmp_path)
    signer = vault_signer(tmp_path, vault)
    await signer.sign_jwt(b"a")
    vault.reject_next_token = True
    await signer.sign_jwt(b"b")
    assert vault.logins == 2


async def test_vault_errors_are_signer_errors(tmp_path: Path) -> None:
    """A failing backend raises SignerError (host becomes unavailable), never a raw httpx error."""
    cfg = VaultTransitSignerConfig.model_validate(
        {
            "kind": "vault-transit",
            "addr": "https://vault.example",
            "key": "app",
            "auth": {"method": "token", "token_file": str(tmp_path / "missing")},
        }
    )
    with pytest.raises(SignerError):
        await VaultTransitSigner(cfg).sign_jwt(b"x")


async def test_vault_import_wraps_the_key_for_transit_byok(tmp_path: Path) -> None:
    """The ciphertext is RSA-OAEP(AES key) || AES-KWP(PKCS#8 DER), and the key is non-exportable."""
    vault = FakeVault(make_key(), tmp_path)
    vault.exists = False
    signer = vault_signer(tmp_path, vault)
    app_key = make_key()
    await signer.import_key(SecretStr(pem_of(app_key)))
    assert vault.imported is not None
    assert vault.imported["path"] == "/v1/transit/keys/app/import"
    assert vault.imported["type"] == "rsa-2048"
    assert vault.imported["exportable"] is False
    blob = base64.b64decode(str(vault.imported["ciphertext"]))
    size = vault.wrapping.key_size // 8
    aes = vault.wrapping.decrypt(
        blob[:size],
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    der = aes_key_unwrap_with_padding(aes, blob[size:])
    restored = serialization.load_der_private_key(der, None)
    assert isinstance(restored, rsa.RSAPrivateKey)
    assert restored.private_numbers() == app_key.private_numbers()


async def test_vault_import_of_an_existing_key_adds_a_version(tmp_path: Path) -> None:
    """Key rotation: an existing Transit key gets `import_version`."""
    vault = FakeVault(make_key(), tmp_path)
    signer = vault_signer(tmp_path, vault)
    await signer.import_key(SecretStr(pem_of(make_key())))
    assert vault.imported is not None
    assert str(vault.imported["path"]).endswith("/import_version")
