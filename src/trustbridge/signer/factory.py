"""Builds signers from their configuration (`SIGNERS`, ADR 0030)."""

from collections.abc import Mapping

from trustbridge.signer.base import AppKeySigner
from trustbridge.signer.config import FileSignerConfig, SignerConfig
from trustbridge.signer.file import FileSigner
from trustbridge.signer.vault import VaultTransitSigner


def build_signer(config: SignerConfig) -> AppKeySigner:
    """Build the signer for one configuration entry."""
    if isinstance(config, FileSignerConfig):
        return FileSigner(config.path)
    return VaultTransitSigner(config)


def build_signers(configs: Mapping[str, SignerConfig]) -> dict[str, AppKeySigner]:
    """Build every configured signer by name (none of them reads a key or calls out yet)."""
    return {name: build_signer(cfg) for name, cfg in configs.items()}
