"""Webhook-secret store interface and the per-process cache (ADR 0030 invariant 11).

A `SecretStore` resolves a `webhook_secret_ref` (the same store serves client secrets and
signer credentials). Refs are `file:/path` (a mounted secret; trailing newline stripped) or
`env:NAME`; an external store (Vault, a cloud secret manager) implements the same Protocol.
`WebhookSecrets` reads a host's secret on first use and caches it in process memory, so a
store outage after one successful read never stops signature verification. A refresh on a
signature mismatch (rotation) is rate limited and keeps the cached value if the store fails.
"""

import asyncio
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


class SecretUnavailable(Exception):  # noqa: N818 -- a result, named for what it says
    """The secret could not be read (store down, ref unknown, empty value)."""


class SecretStore(Protocol):
    """Resolves a secret reference to its value."""

    async def read(self, ref: str) -> str:
        """Return the secret for `ref`; raise `SecretUnavailable` if it cannot be read."""
        ...


class FileEnvSecretStore:
    """`file:<path>` and `env:<NAME>` references."""

    async def read(self, ref: str) -> str:
        """Read the referenced file or environment variable."""
        scheme, _, target = ref.partition(":")
        if scheme == "env":
            value = os.environ.get(target, "")
        elif scheme == "file":
            try:
                value = await asyncio.to_thread(Path(target).read_text, "utf-8")
            except OSError as exc:
                raise SecretUnavailable("secret file unreadable") from exc
        else:
            raise SecretUnavailable("unknown secret reference scheme")
        value = value.rstrip("\r\n")
        if not value:
            raise SecretUnavailable("secret is empty")
        return value


class WebhookSecrets:
    """Per-host webhook secrets, resolved on first use and cached in process memory."""

    def __init__(
        self,
        store: SecretStore,
        *,
        read_timeout: float,
        refresh_min_seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind the store, the per-read timeout and the minimum gap between refreshes."""
        self._store = store
        self._read_timeout = read_timeout
        self._refresh_min = refresh_min_seconds
        self._monotonic = monotonic
        self._cache: dict[str, tuple[bytes, float]] = {}
        self._refs: dict[str, str] = {}

    async def _read(self, ref: str) -> bytes | None:
        """One bounded store read; None on any failure (the failure is logged without ref)."""
        try:
            async with asyncio.timeout(self._read_timeout):
                return (await self._store.read(ref)).encode()
        except (SecretUnavailable, TimeoutError):
            return None
        except Exception:
            logger.exception("secret store read failed")
            return None

    async def get(self, host: str, ref: str) -> bytes | None:
        """The secret of `host`: cached, else read now. None when it cannot be read."""
        cached = self._cache.get(host)
        if cached is not None and self._refs.get(host) == ref:
            return cached[0]
        value = await self._read(ref)
        if value is not None:
            self._cache[host] = (value, self._monotonic())
            self._refs[host] = ref
        return value

    async def refreshed(self, host: str, ref: str) -> bytes | None:
        """Re-read the secret after a signature mismatch, at most every `refresh_min` seconds.

        Returns the new value only if it differs from the cached one; None means "nothing
        new to try" (too soon, store failure or unchanged). A failure keeps the cache.
        """
        cached = self._cache.get(host)
        if cached is not None and self._monotonic() - cached[1] < self._refresh_min:
            return None
        value = await self._read(ref)
        if value is None:
            return None
        previous = cached[0] if cached is not None else None
        self._cache[host] = (value, self._monotonic())
        self._refs[host] = ref
        return value if value != previous else None

    async def warm(self, refs: dict[str, str]) -> None:
        """Best-effort cache warming at startup (`{host: ref}`); failures are ignored."""
        for host, ref in refs.items():
            await self.get(host, ref)
