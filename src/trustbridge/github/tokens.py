"""Installation access tokens: minted with the signer-backed App JWT, cached, down-scoped.

A token is cached in process until about 5 minutes before its 1-hour expiry, per
`(host, installation, repositories, permissions)`, so a down-scoped token is never reused for a
wider request. `POST /app/installations/{id}/access_tokens` takes `repository_ids` and
`permissions` for down-scoping. Concurrent callers for one key share a single mint.
"""

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from trustbridge.github.app_jwt import build_app_jwt
from trustbridge.github.availability import HostMonitor
from trustbridge.github.client import GitHubApi, GitHubRequester
from trustbridge.github.hosts import GitHubHostConfig
from trustbridge.signer.base import AppKeySigner

REFRESH_MARGIN_SECONDS = 5 * 60
"""A cached token is replaced once it is within this many seconds of expiry."""

_CacheKey = tuple[str, int, tuple[int, ...] | None, tuple[tuple[str, str], ...] | None]


@dataclass(frozen=True, slots=True)
class _Cached:
    token: str
    expires_at: float


def _parse_expiry(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class InstallationTokenProvider:
    """Mints and caches installation tokens for ready hosts."""

    def __init__(
        self,
        monitor: HostMonitor,
        signers: Mapping[str, AppKeySigner],
        api_factory: Callable[[GitHubHostConfig], GitHubRequester] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """`monitor` supplies host status; `api_factory` and `clock` are replaceable for tests."""
        self._monitor = monitor
        self._signers = signers
        self._api_factory = api_factory or GitHubApi
        self._clock = clock
        self._cache: dict[_CacheKey, _Cached] = {}
        self._locks: dict[_CacheKey, asyncio.Lock] = {}

    async def token(
        self,
        host: str,
        installation_id: int,
        *,
        repository_ids: Sequence[int] | None = None,
        permissions: Mapping[str, str] | None = None,
    ) -> str:
        """Return a valid token, minting one when none is cached or it is about to expire.

        Raises `HostNotReadyError` for a host that is not ready; no call is made for it.
        """
        cfg = self._monitor.require_ready(host)
        key: _CacheKey = (
            host,
            installation_id,
            tuple(sorted(repository_ids)) if repository_ids is not None else None,
            tuple(sorted(permissions.items())) if permissions is not None else None,
        )
        async with self._locks.setdefault(key, asyncio.Lock()):
            cached = self._cache.get(key)
            if cached is not None and cached.expires_at - self._clock() > REFRESH_MARGIN_SECONDS:
                return cached.token
            minted = await self._mint(cfg, installation_id, repository_ids, permissions)
            self._cache[key] = minted
            return minted.token

    def invalidate(self, host: str, installation_id: int) -> None:
        """Drop every cached token of an installation (it was deleted, suspended or changed)."""
        for key in [k for k in self._cache if k[0] == host and k[1] == installation_id]:
            del self._cache[key]

    async def _mint(
        self,
        cfg: GitHubHostConfig,
        installation_id: int,
        repository_ids: Sequence[int] | None,
        permissions: Mapping[str, str] | None,
    ) -> _Cached:
        signer = self._signers[cfg.signer]
        if cfg.app_id is None:  # pragma: no cover -- require_ready excludes unregistered hosts
            msg = "host has no App id"
            raise ValueError(msg)
        jwt = await build_app_jwt(signer, cfg.app_id, now=self._clock())
        body: dict[str, object] = {}
        if repository_ids is not None:
            body["repository_ids"] = list(repository_ids)
        if permissions is not None:
            body["permissions"] = dict(permissions)
        resp = await self._api_factory(cfg).request_as_app(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            jwt=jwt,
            json=body or None,
        )
        return _Cached(str(resp.data["token"]), _parse_expiry(str(resp.data["expires_at"])))
