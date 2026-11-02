"""The signer interface (ADR 0030): sign-only, so the App key can stay in Vault or a KMS."""

from typing import Protocol, runtime_checkable

from pydantic import SecretStr


class SignerError(Exception):
    """A signer failed (backend unreachable, key unusable). Never carries key material."""


class SignerImportUnsupportedError(SignerError):
    """The signer cannot import a key (the manifest flow then cannot hand it a PEM)."""


@runtime_checkable
class AppKeySigner(Protocol):
    """Signs the GitHub App JWT with RS256 without exposing the private key."""

    @property
    def key_id(self) -> str:
        """A stable id of the key (fingerprint or KMS key id)."""
        ...

    async def sign_jwt(self, signing_input: bytes) -> bytes:
        """Return the RSASSA-PKCS1-v1_5 SHA-256 signature over `header.payload`."""
        ...

    async def public_key_pem(self) -> str:
        """Return the public key (SubjectPublicKeyInfo PEM), for the self-test."""
        ...


@runtime_checkable
class KeyImporter(Protocol):
    """An operation of a signer implementation outside `AppKeySigner`, which stays sign-only.

    The manifest flow hands the PEM GitHub returns straight to this and never logs or returns it.
    A signer without it raises `SignerImportUnsupportedError` (or does not implement it) and the
    registration flow reports that the key must be imported out of band.
    """

    async def import_key(self, pem: SecretStr) -> None:
        """Import the private key (replacing the stored one for the file kind)."""
        ...
