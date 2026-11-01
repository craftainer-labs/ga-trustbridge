"""Fixtures shared by the trustbridge unit tests."""

import pytest

from tests.trustbridge.github.conftest import KeySigner


@pytest.fixture
def key_signers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build the `dev` signer of `helpers.SIGNERS` as an in-memory RSA key instead of a file."""
    monkeypatch.setattr(
        "trustbridge.github.runtime.build_signers", lambda _configs: {"dev": KeySigner()}
    )
