"""Webhook secret resolution with a process-local cache (ADR 0030, invariant 11).

Secrets are resolved on first use and cached in memory, so a secret-store or Vault outage after
a successful read does not stop signature verification or ingress. A process with no cached
secret for a host raises `WebhookSecretUnavailableError`, which the ingress answers with 503
(the delivery is redelivered by the reconciler once the host is ready). An unregistered host
raises `HostUnregisteredError` (also 503, body unread). The ingress calls `secret_for`.

A signature mismatch may mean the secret was rotated: `refreshed` re-reads it, at most every
`refresh_min_seconds`, and returns the new value only if it differs from the cached one. A
failing store keeps the cached value.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Iterable

from trustbridge.github.availability import HostDirectory
from trustbridge.github.ports import SecretReader

logger = logging.getLogger(__name__)


class HostUnregisteredError(Exception):
    """The host has no App yet, so it has no webhook secret."""


class WebhookSecretUnavailableError(Exception):
    """The secret is not cached and the secret store could not be read."""


class WebhookSecretResolver:
    """Resolves and caches the webhook secret of each host."""

    def __init__(
        self,
        hosts: HostDirectory,
        reader: SecretReader,
        *,
        read_timeout: float = 5.0,
        refresh_min_seconds: float = 60.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Use `hosts` for the reference and `reader` for the secret store (reads are bounded)."""
        self._hosts = hosts
        self._reader = reader
        self._read_timeout = read_timeout
        self._refresh_min = refresh_min_seconds
        self._monotonic = monotonic
        self._cache: dict[tuple[str, str], tuple[bytes, float]] = {}

    def _ref(self, host: str) -> str:
        """The host's secret reference, or `HostUnregisteredError`."""
        ref = self._hosts.config(host).webhook_secret_ref
        if ref is None:
            raise HostUnregisteredError(host)
        return ref

    async def _read(self, ref: str) -> bytes:
        """One bounded store read; any failure becomes `WebhookSecretUnavailableError`."""
        try:
            async with asyncio.timeout(self._read_timeout):
                return (await self._reader.read(ref)).get_secret_value().encode()
        except Exception as exc:
            msg = "webhook secret cannot be read"
            raise WebhookSecretUnavailableError(msg) from exc

    async def secret_for(self, host: str) -> bytes:
        """Return the host's webhook secret, from the cache when present."""
        ref = self._ref(host)
        cached = self._cache.get((host, ref))
        if cached is not None:
            return cached[0]
        value = await self._read(ref)
        self._cache[(host, ref)] = (value, self._monotonic())
        return value

    async def refreshed(self, host: str) -> bytes | None:
        """Re-read the secret after a signature mismatch (rotation), rate limited.

        Returns the new value only if it differs from the cached one; None means "nothing new
        to try" (too soon, store failure or unchanged). A failure keeps the cache.
        """
        ref = self._ref(host)
        cached = self._cache.get((host, ref))
        if cached is not None and self._monotonic() - cached[1] < self._refresh_min:
            return None
        try:
            value = await self._read(ref)
        except WebhookSecretUnavailableError:
            return None
        self._cache[(host, ref)] = (value, self._monotonic())
        return value if cached is None or value != cached[0] else None

    async def warm(self, names: Iterable[str]) -> None:
        """Best-effort cache warming at startup; failures are ignored (read on first use)."""
        for name in names:
            try:
                await self.secret_for(name)
            except (HostUnregisteredError, WebhookSecretUnavailableError):
                logger.info("webhook secret not warmed", extra={"host": name})
