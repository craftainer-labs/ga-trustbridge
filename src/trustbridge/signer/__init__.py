"""Pluggable GitHub App key signers (ADR 0030): the `file` and `vault-transit` kinds."""

from trustbridge.signer.base import (
    AppKeySigner,
    KeyImporter,
    SignerError,
    SignerImportUnsupportedError,
)
from trustbridge.signer.config import SignerConfig, parse_signers
from trustbridge.signer.factory import build_signer, build_signers

__all__ = [
    "AppKeySigner",
    "KeyImporter",
    "SignerConfig",
    "SignerError",
    "SignerImportUnsupportedError",
    "build_signer",
    "build_signers",
    "parse_signers",
]
