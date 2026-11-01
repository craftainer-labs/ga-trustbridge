"""The `vault-transit` signer: signs with a non-exportable Vault Transit RSA key (ADR 0030).

Signing is `POST /v1/<mount>/sign/<key>/sha2-256` with `signature_algorithm=pkcs1v15` and
`prehashed=false`, so Vault hashes the JWT signing input itself and returns
`vault:v<N>:<base64>`; the base64 part is the raw PKCS#1 v1.5 signature. Authentication is the
Kubernetes or AppRole auth method, or a token file; the token is cached until shortly before
its lease ends and a 403 triggers one fresh login.
"""

import asyncio
import base64
import os
import ssl
import time
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.keywrap import aes_key_wrap_with_padding
from pydantic import SecretStr

from trustbridge.signer.base import SignerError
from trustbridge.signer.config import (
    VaultKubernetesAuth,
    VaultTokenAuth,
    VaultTransitSignerConfig,
)

_REQUEST_TIMEOUT = 10.0
_TOKEN_REFRESH_MARGIN = 30.0
_FORBIDDEN = 403
_NOT_FOUND = 404
_AES_KEY_BYTES = 32


def _read_file(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError as exc:
        msg = f"cannot read credential file {path}"
        raise SignerError(msg) from exc


class VaultTransitSigner:
    """`AppKeySigner` over Vault Transit. `client` is injectable for tests."""

    def __init__(
        self,
        config: VaultTransitSignerConfig,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Build the signer; an HTTP client is created lazily with the configured CA."""
        self._cfg = config
        self._client = client
        self._token: str | None = None
        self._token_expires = 0.0
        self._lock = asyncio.Lock()
        self.key_id = f"vault-transit:{config.mount}/{config.key}"

    # ---- HTTP -----------------------------------------------------------
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            verify: ssl.SSLContext | bool = True
            if self._cfg.ca_bundle is not None:
                verify = ssl.create_default_context(cafile=str(self._cfg.ca_bundle))
            self._client = httpx.AsyncClient(
                base_url=self._cfg.addr, timeout=_REQUEST_TIMEOUT, verify=verify
            )
        return self._client

    def _headers(self, token: str | None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if token is not None:
            headers["X-Vault-Token"] = token
        if self._cfg.namespace is not None:
            headers["X-Vault-Namespace"] = self._cfg.namespace
        return headers

    async def _login(self) -> str:
        auth = self._cfg.auth
        if isinstance(auth, VaultTokenAuth):
            self._token_expires = float("inf")
            return await asyncio.to_thread(_read_file, auth.token_file)
        if isinstance(auth, VaultKubernetesAuth):
            body = {"role": auth.role, "jwt": await asyncio.to_thread(_read_file, auth.jwt_file)}
        else:
            secret_id = await asyncio.to_thread(_read_file, auth.secret_id_file)
            body = {"role_id": auth.role_id, "secret_id": secret_id}
        resp = await self._http().post(
            f"/v1/auth/{auth.auth_mount}/login", json=body, headers=self._headers(None)
        )
        if resp.status_code != httpx.codes.OK:
            msg = f"vault login failed with HTTP {resp.status_code}"
            raise SignerError(msg)
        data = resp.json()["auth"]
        self._token_expires = time.monotonic() + float(data.get("lease_duration", 0))
        return str(data["client_token"])

    async def _get_token(self, *, force: bool = False) -> str:
        async with self._lock:
            fresh = time.monotonic() < self._token_expires - _TOKEN_REFRESH_MARGIN
            if self._token is None or force or not fresh:
                self._token = await self._login()
            return self._token

    async def _request(
        self, method: str, path: str, json: dict[str, Any] | None = None
    ) -> httpx.Response:
        for attempt in (0, 1):
            token = await self._get_token(force=attempt == 1)
            try:
                resp = await self._http().request(
                    method, path, json=json, headers=self._headers(token)
                )
            except httpx.HTTPError as exc:
                msg = f"vault request failed: {type(exc).__name__}"
                raise SignerError(msg) from exc
            if resp.status_code != _FORBIDDEN or attempt == 1:
                return resp
        raise AssertionError  # pragma: no cover -- the loop always returns

    # ---- AppKeySigner ---------------------------------------------------
    async def sign_jwt(self, signing_input: bytes) -> bytes:
        """Return the RS256 signature from Vault Transit."""
        resp = await self._request(
            "POST",
            f"/v1/{self._cfg.mount}/sign/{self._cfg.key}/sha2-256",
            {
                "input": base64.b64encode(signing_input).decode(),
                "signature_algorithm": "pkcs1v15",
                "prehashed": False,
            },
        )
        if resp.status_code != httpx.codes.OK:
            msg = f"vault transit sign failed with HTTP {resp.status_code}"
            raise SignerError(msg)
        signature = str(resp.json()["data"]["signature"])
        # "vault:v1:<base64>" -> the raw signature bytes.
        return base64.b64decode(signature.rsplit(":", 1)[-1])

    async def public_key_pem(self) -> str:
        """Return the latest version's public key PEM."""
        resp = await self._request("GET", f"/v1/{self._cfg.mount}/keys/{self._cfg.key}")
        if resp.status_code != httpx.codes.OK:
            msg = f"vault transit key read failed with HTTP {resp.status_code}"
            raise SignerError(msg)
        data = resp.json()["data"]
        latest = str(data["latest_version"])
        return str(data["keys"][latest]["public_key"])

    # ---- KeyImporter ----------------------------------------------------
    async def import_key(self, pem: SecretStr) -> None:
        """Import the manifest flow's PEM through Transit BYOK wrapping (non-exportable)."""
        private = serialization.load_pem_private_key(pem.get_secret_value().encode(), password=None)
        if not isinstance(private, rsa.RSAPrivateKey):
            msg = "the App key must be an RSA key"
            raise SignerError(msg)
        wrap_resp = await self._request("GET", f"/v1/{self._cfg.mount}/wrapping_key")
        if wrap_resp.status_code != httpx.codes.OK:
            msg = f"vault wrapping key read failed with HTTP {wrap_resp.status_code}"
            raise SignerError(msg)
        wrapping = serialization.load_pem_public_key(
            str(wrap_resp.json()["data"]["public_key"]).encode()
        )
        if not isinstance(wrapping, rsa.RSAPublicKey):
            msg = "vault returned a wrapping key that is not RSA"
            raise SignerError(msg)
        ephemeral = os.urandom(_AES_KEY_BYTES)
        der = private.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        wrapped_target = aes_key_wrap_with_padding(ephemeral, der)
        wrapped_aes = wrapping.encrypt(
            ephemeral,
            padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
        body: dict[str, Any] = {
            "ciphertext": base64.b64encode(wrapped_aes + wrapped_target).decode(),
            "hash_function": "SHA256",
        }
        existing = await self._request("GET", f"/v1/{self._cfg.mount}/keys/{self._cfg.key}")
        if existing.status_code == _NOT_FOUND:
            path = f"/v1/{self._cfg.mount}/keys/{self._cfg.key}/import"
            body["type"] = f"rsa-{private.key_size}"
            body["exportable"] = False
        else:
            path = f"/v1/{self._cfg.mount}/keys/{self._cfg.key}/import_version"
        resp = await self._request("POST", path, body)
        if resp.status_code not in {httpx.codes.OK, httpx.codes.NO_CONTENT}:
            msg = f"vault transit import failed with HTTP {resp.status_code}"
            raise SignerError(msg)

    async def aclose(self) -> None:
        """Close the HTTP client if this signer created it."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
