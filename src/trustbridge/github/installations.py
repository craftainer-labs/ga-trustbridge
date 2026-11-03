"""Reads an installation's current state from GitHub with the App JWT.

`installation.*` webhooks are triggers, not the source of truth: GitHub documents no ordering
of deliveries and no timestamp on the `installation` payload, so a retried `created` can arrive
after a later `deleted`. The handlers therefore re-read the installation here before applying
an event (`GET /app/installations/{installation_id}`; `suspended_at` is documented on the
response, and a missing installation answers 404). Documentation checked (2026-11-02), none of
which promises delivery order or a payload timestamp:

- https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks
- https://docs.github.com/en/webhooks/using-webhooks/handling-webhook-deliveries
- https://docs.github.com/en/webhooks/webhook-events-and-payloads
- https://docs.github.com/en/rest/apps/apps#get-an-installation-for-the-authenticated-app
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from trustbridge.github.app_jwt import build_app_jwt
from trustbridge.github.availability import HostMonitor
from trustbridge.github.client import HTTP_NOT_FOUND, GitHubApi, GitHubApiError, GitHubRequester
from trustbridge.github.hosts import GitHubHostConfig
from trustbridge.signer.base import AppKeySigner


@dataclass(frozen=True, slots=True)
class InstallationInfo:
    """What `GET /app/installations/{id}` says about an installation that exists."""

    login: str
    account_id: int
    suspended: bool


class AppInstallationLookup:
    """`InstallationLookup` over the App JWT (the signer holds the key)."""

    def __init__(
        self,
        monitor: HostMonitor,
        signers: Mapping[str, AppKeySigner],
        api_factory: Callable[[GitHubHostConfig], GitHubRequester] | None = None,
    ) -> None:
        """`api_factory` is replaceable for tests."""
        self._monitor = monitor
        self._signers = signers
        self._api_factory = api_factory or GitHubApi

    async def fetch(self, host: str, installation_id: int) -> InstallationInfo | None:
        """The installation's current state, or None when GitHub answers 404 (it is gone).

        Raises `HostNotReadyError` for a host that is not ready and `GitHubApiError` for every
        other failure, so the worker counts a failure and retries the delivery.
        """
        cfg = self._monitor.require_ready(host)
        if cfg.app_id is None:  # pragma: no cover -- require_ready excludes unregistered hosts
            msg = "host has no App id"
            raise ValueError(msg)
        jwt = await build_app_jwt(self._signers[cfg.signer], cfg.app_id)
        try:
            resp = await self._api_factory(cfg).request_as_app(
                "GET", f"/app/installations/{installation_id}", jwt=jwt
            )
        except GitHubApiError as exc:
            if exc.status == HTTP_NOT_FOUND:
                return None
            raise
        account = resp.data.get("account") or {}
        return InstallationInfo(
            login=str(account.get("login", "")),
            account_id=int(account.get("id", 0)),
            suspended=resp.data.get("suspended_at") is not None,
        )
