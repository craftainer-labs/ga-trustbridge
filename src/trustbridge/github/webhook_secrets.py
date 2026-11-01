"""Webhook secret resolution with a process-local cache (ADR 0030, invariant 11).

Secrets are resolved on first use and cached in memory, so a secret-store or Vault outage after
a successful read does not stop signature verification or ingress. A process with no cached
secret for a host raises `WebhookSecretUnavailableError`, which the ingress answers with 503
(the delivery is redelivered by the reconciler once the host is ready). An unregistered host
raises `HostUnregisteredError` (also 503, body unread). NG3's ingress calls `secret_for`.
"""

from trustbridge.github.availability import HostDirectory, HostState
from trustbridge.github.ports import SecretReader


class HostUnregisteredError(Exception):
    """The host has no App yet, so it has no webhook secret."""


class WebhookSecretUnavailableError(Exception):
    """The secret is not cached and the secret store could not be read."""


class WebhookSecretResolver:
    """Resolves and caches the webhook secret of each host."""

    def __init__(self, hosts: HostDirectory, reader: SecretReader) -> None:
        """Use `hosts` for the reference and `reader` for the secret store."""
        self._hosts = hosts
        self._reader = reader
        self._cache: dict[tuple[str, str], bytes] = {}

    async def secret_for(self, host: str) -> bytes:
        """Return the host's webhook secret, from the cache when present."""
        if self._hosts.status(host).state is HostState.UNREGISTERED:
            raise HostUnregisteredError(host)
        ref = self._hosts.config(host).webhook_secret_ref
        if ref is None:
            raise HostUnregisteredError(host)
        cached = self._cache.get((host, ref))
        if cached is not None:
            return cached
        try:
            value = (await self._reader.read(ref)).get_secret_value().encode()
        except Exception as exc:
            msg = f"webhook secret of host {host!r} cannot be read"
            raise WebhookSecretUnavailableError(msg) from exc
        self._cache[(host, ref)] = value
        return value
