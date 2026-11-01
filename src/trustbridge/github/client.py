"""A thin githubkit facade: REST calls against one host with an App JWT or an installation token.

githubkit does everything after the token exists (ADR 0030): base URL (the GHES `/api/v3` suffix
comes from the host config), TLS (the host's CA bundle), transport (injectable for tests) and
errors. Calls are raw REST (`arequest`), so a path in a log or test is the path GitHub documents.
"""

import ssl
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

import httpx2
from githubkit import GitHub, TokenAuthStrategy
from githubkit.exception import RequestError, RequestFailed

from trustbridge.github.hosts import GitHubHostConfig

_DEFAULT_TIMEOUT = 15.0
HTTP_NOT_FOUND = 404
HTTP_UNPROCESSABLE = 422
_RATE_LIMITED = frozenset({403, 429})
_SERVER_ERROR = 500


class GitHubApiError(Exception):
    """A GitHub call failed. `status` is the HTTP status (0 for a network error)."""

    def __init__(self, status: int, message: str) -> None:
        """Record the status and an audit-safe message (never a token)."""
        super().__init__(f"GitHub API error {status}: {message}")
        self.status = status
        self.message = message

    @property
    def retryable(self) -> bool:
        """True for network errors, rate limits and 5xx."""
        return self.status == 0 or self.status in _RATE_LIMITED or self.status >= _SERVER_ERROR


@dataclass(frozen=True, slots=True)
class ApiResponse:
    """A decoded response: HTTP status and JSON body (None when empty)."""

    status: int
    data: Any
    link: str | None = None
    """The `Link` response header (pagination), when present."""


class GitHubRequester(Protocol):
    """What the GitHub core's writers, reporters and token provider need."""

    async def request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> ApiResponse:
        """Call `path` with an installation token."""
        ...

    async def request_as_app(
        self,
        method: str,
        path: str,
        *,
        jwt: str,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> ApiResponse:
        """Call `path` with an App JWT (sent as `Authorization: Bearer`)."""
        ...

    async def request_unauthenticated(
        self, method: str, path: str, *, json: Mapping[str, Any] | None = None
    ) -> ApiResponse:
        """Call `path` with no credentials (the manifest code conversion)."""
        ...


def _options(
    json: Mapping[str, Any] | None,
    params: Mapping[str, Any] | None,
    headers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if json is not None:
        out["json"] = dict(json)
    if params is not None:
        out["params"] = dict(params)
    if headers is not None:
        out["headers"] = dict(headers)
    return out


class GitHubApi:
    """`GitHubRequester` for one host, built on githubkit."""

    def __init__(
        self,
        host: GitHubHostConfig,
        *,
        async_transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        """Remember the host; `async_transport` replaces the network (tests)."""
        self._host = host
        self._transport = async_transport

    @asynccontextmanager
    async def _client(self, token: str | None) -> AsyncIterator[GitHub[Any]]:
        ssl_verify: bool | ssl.SSLContext = True
        if self._host.ca_bundle is not None:
            ssl_verify = ssl.create_default_context(cafile=str(self._host.ca_bundle))
        opts: dict[str, Any] = {
            "base_url": self._host.api_url,
            "http_cache": False,
            "auto_retry": False,
            "follow_redirects": False,
            "ssl_verify": ssl_verify,
            "timeout": httpx2.Timeout(_DEFAULT_TIMEOUT),
            "async_transport": self._transport,
        }
        github = GitHub(**opts) if token is None else GitHub(TokenAuthStrategy(token), **opts)
        async with github as client:
            yield client

    async def _send(
        self,
        client: GitHub[Any],
        method: str,
        path: str,
        options: Mapping[str, Any],
    ) -> ApiResponse:
        """`options` holds the optional `json`, `params` and `headers` of the request."""
        try:
            resp = await client.arequest(method, path, **options)
        except RequestFailed as exc:
            raise GitHubApiError(exc.response.status_code, f"{method} {path}") from exc
        except (RequestError, httpx2.HTTPError) as exc:
            raise GitHubApiError(0, f"{method} {path}: {type(exc).__name__}") from exc
        return ApiResponse(
            resp.status_code, resp.json() if resp.text else None, resp.headers.get("link")
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> ApiResponse:
        """Call `path` with an installation token (githubkit's token auth strategy)."""
        async with self._client(token) as client:
            return await self._send(client, method, path, _options(json, params))

    async def request_as_app(
        self,
        method: str,
        path: str,
        *,
        jwt: str,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> ApiResponse:
        """Call `path` with an App JWT; GitHub requires `Bearer` for JWTs."""
        async with self._client(None) as client:
            return await self._send(
                client,
                method,
                path,
                _options(json, params, {"Authorization": f"Bearer {jwt}"}),
            )

    async def request_unauthenticated(
        self, method: str, path: str, *, json: Mapping[str, Any] | None = None
    ) -> ApiResponse:
        """Call `path` with no credentials: `POST /app-manifests/{code}/conversions`."""
        async with self._client(None) as client:
            return await self._send(client, method, path, _options(json, None))
