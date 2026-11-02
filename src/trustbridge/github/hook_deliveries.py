"""GitHub's hook-delivery endpoints for a host's App (`HookDeliveryClient` of the reconciler).

`GET /app/hook/deliveries` lists delivery attempts newest first (cursor pagination through the
`Link` header) and `POST /app/hook/deliveries/{id}/attempts` redelivers one. Both authenticate
as the App (JWT from the host's signer), so a host that is not `ready` is never called.
"""

import re
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from trustbridge.github.app_jwt import build_app_jwt
from trustbridge.github.availability import HostMonitor
from trustbridge.github.client import GitHubApi, GitHubRequester
from trustbridge.github.hosts import GitHubHostConfig
from trustbridge.signer.base import AppKeySigner
from trustbridge.worker.reconciler import HookDelivery

PAGE_SIZE = 100
_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


def next_cursor(link: str | None) -> str | None:
    """The `cursor` of the `rel="next"` URL in a `Link` header, if any."""
    match = _NEXT.search(link or "")
    if match is None:
        return None
    values = parse_qs(urlsplit(match.group(1)).query).get("cursor")
    return values[0] if values else None


class GitHubHookDeliveries:
    """`HookDeliveryClient` over the GitHub REST API."""

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

    async def _jwt(self, host: GitHubHostConfig) -> str:
        cfg = self._monitor.require_ready(host.name)
        # A ready host is registered, so it has an App id.
        return await build_app_jwt(self._signers[cfg.signer], cfg.app_id or 0)

    async def list_deliveries(
        self, host: GitHubHostConfig, *, since: datetime
    ) -> AsyncIterator[HookDelivery]:
        """Attempts newest first, until one was delivered before `since`."""
        api = self._api_factory(self._monitor.config(host.name))
        cursor: str | None = None
        while True:
            params: dict[str, object] = {"per_page": PAGE_SIZE}
            if cursor is not None:
                params["cursor"] = cursor
            resp = await api.request_as_app(
                "GET", "/app/hook/deliveries", jwt=await self._jwt(host), params=params
            )
            for item in resp.data or []:
                delivered = datetime.fromisoformat(item["delivered_at"].replace("Z", "+00:00"))
                if delivered < since:
                    return
                yield HookDelivery(
                    id=int(item["id"]),
                    guid=str(item["guid"]),
                    status_code=int(item["status_code"]),
                    delivered_at=delivered,
                    redelivery=bool(item.get("redelivery", False)),
                )
            cursor = next_cursor(resp.link)
            if cursor is None:
                return

    async def redeliver(self, host: GitHubHostConfig, hook_delivery_id: int) -> None:
        """`POST /app/hook/deliveries/{id}/attempts`."""
        api = self._api_factory(self._monitor.config(host.name))
        await api.request_as_app(
            "POST",
            f"/app/hook/deliveries/{hook_delivery_id}/attempts",
            jwt=await self._jwt(host),
        )
