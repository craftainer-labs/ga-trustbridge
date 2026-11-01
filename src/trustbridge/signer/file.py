"""The `file` signer: a PEM on disk. For development only (refused when `mode=production`)."""

import asyncio
import hashlib
import os
import stat
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pydantic import SecretStr

from trustbridge.signer.base import SignerError

_MIN_RSA_BITS = 2048
_GROUP_OTHER_BITS = 0o077
_PRIVATE_MODE = 0o600


def _load_private_key(pem: bytes) -> rsa.RSAPrivateKey:
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError) as exc:
        msg = "the file does not contain an unencrypted PEM private key"
        raise SignerError(msg) from exc
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < _MIN_RSA_BITS:
        msg = f"the key must be RSA of at least {_MIN_RSA_BITS} bits"
        raise SignerError(msg)
    return key


def public_key_fingerprint(public_pem: bytes) -> str:
    """Return a stable `sha256:` fingerprint of the SubjectPublicKeyInfo DER."""
    pub = serialization.load_pem_public_key(public_pem)
    der = pub.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return "sha256:" + hashlib.sha256(der).hexdigest()


class FileSigner:
    """Signs with a private key read from a PEM file whose permissions are checked.

    The key is read on first use, so a host that is still unregistered (no file yet) can be
    configured; the manifest flow's `import_key` then creates the file.
    """

    def __init__(self, path: Path) -> None:
        """Remember the path; nothing is read yet."""
        self._path = path
        self._key: rsa.RSAPrivateKey | None = None

    def _load(self) -> rsa.RSAPrivateKey:
        if self._key is None:
            self._key = self._read()
        return self._key

    def _read(self) -> rsa.RSAPrivateKey:
        try:
            mode = stat.S_IMODE(self._path.stat().st_mode)
            if mode & _GROUP_OTHER_BITS:
                msg = f"{self._path} is accessible by group or others (mode {mode:o}); use 0600"
                raise SignerError(msg)
            return _load_private_key(self._path.read_bytes())
        except OSError as exc:
            msg = f"cannot read the signer key file {self._path}"
            raise SignerError(msg) from exc

    def _public_pem(self) -> bytes:
        return (
            self._load()
            .public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        )

    @property
    def key_id(self) -> str:
        """The `sha256:` fingerprint of the public key (reads the file on first use)."""
        return public_key_fingerprint(self._public_pem())

    async def sign_jwt(self, signing_input: bytes) -> bytes:
        """Return the RS256 signature."""
        return self._load().sign(signing_input, padding.PKCS1v15(), hashes.SHA256())

    async def public_key_pem(self) -> str:
        """Return the public key PEM."""
        return self._public_pem().decode()

    async def import_key(self, pem: SecretStr) -> None:
        """Write the PEM with mode 0600 (the manifest flow's key), then use it."""
        raw = pem.get_secret_value().encode()
        key = _load_private_key(raw)
        await asyncio.to_thread(self._write, raw)
        self._key = key

    def _write(self, raw: bytes) -> None:
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _PRIVATE_MODE)
        os.fchmod(fd, _PRIVATE_MODE)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
