"""Libsodium sealed-box encryption of Actions secrets (GitHub's required scheme, PyNaCl)."""

import base64

from nacl.public import PublicKey, SealedBox


def seal(public_key_b64: str, value: str) -> str:
    """Encrypt `value` to the repository's (or org's) public key; return base64 ciphertext."""
    box = SealedBox(PublicKey(base64.b64decode(public_key_b64)))
    return base64.b64encode(box.encrypt(value.encode())).decode()
