"""Fixtures shared by the trustbridge unit tests."""

import pytest

from tests.trustbridge.github.conftest import KeySigner


@pytest.fixture(autouse=True)
def _clear_oidc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the devcontainer's http:// OIDC_* settings, which would override production kwargs."""
    for name in ("OIDC_ISSUER", "OIDC_ISSUER_URL", "OIDC_DISCOVERY_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def key_signers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build the `dev` signer of `helpers.SIGNERS` as an in-memory RSA key instead of a file."""
    monkeypatch.setattr(
        "trustbridge.github.runtime.build_signers", lambda _configs: {"dev": KeySigner()}
    )
