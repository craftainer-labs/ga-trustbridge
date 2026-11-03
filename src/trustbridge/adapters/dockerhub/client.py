"""A thin httpx client for the Docker Hub API (no business rules).

Authentication follows the Docker Hub API reference: `POST /v2/auth/token` exchanges a username
and a personal access token for a short-lived bearer JWT, which every other call then sends.
"""

from types import TracebackType
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import SecretStr

from trustbridge.adapters.interface import AdapterError

JsonObj = dict[str, Any]

DEFAULT_TIMEOUT_SECONDS = 15.0
HTTP_REQUEST_TIMEOUT = 408
HTTP_TOO_EARLY = 425
HTTP_TOO_MANY_REQUESTS = 429
HTTP_SERVER_ERROR = 500
AUTH_PATH = "/v2/auth/token"


def seg(value: str) -> str:
    """Percent-encode one URL path segment (names never smuggle a `/` or `..`)."""
    return quote(value, safe="")


def retryable_status(status: int) -> bool:
    """Return whether an HTTP status is worth retrying."""
    return status in {HTTP_REQUEST_TIMEOUT, HTTP_TOO_EARLY, HTTP_TOO_MANY_REQUESTS} or (
        status >= HTTP_SERVER_ERROR
    )


class DockerHubClient:
    """One short-lived HTTP session against a Docker Hub URL, authenticated by a PAT exchange.

    Error messages carry the method, the path and the status, never a body or a header, so no
    secret can reach an `AdapterError` (invariant A5).
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        token: SecretStr,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """Build the session; nothing is sent until `call`."""
        self._base = base_url.rstrip("/")
        self._username = username
        self._pat = token
        self._bearer: str | None = None
        self._http = httpx.AsyncClient(
            headers={"Accept": "application/json"},
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
        )

    async def __aenter__(self) -> DockerHubClient:
        """Enter the session."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the session."""
        await self._http.aclose()

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json: JsonObj | None = None,
        params: dict[str, str] | None = None,
        accept: frozenset[int] = frozenset(),
        bearer: str | None = None,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {bearer}"} if bearer is not None else {}
        try:
            response = await self._http.request(
                method, f"{self._base}{path}", json=json, params=params, headers=headers
            )
        except httpx.HTTPError as exc:
            msg = f"Docker Hub {method} {path} failed: {type(exc).__name__}"
            raise AdapterError(True, msg) from None
        status = response.status_code
        if response.is_success or status in accept:
            return response
        msg = f"Docker Hub {method} {path} failed with HTTP {status}"
        raise AdapterError(retryable_status(status), msg)

    async def _login(self) -> str:
        body: JsonObj = {"identifier": self._username, "secret": self._pat.get_secret_value()}
        data = json_object(await self._send("POST", AUTH_PATH, json=body))
        bearer = data.get("access_token")
        if not isinstance(bearer, str) or not bearer:
            raise AdapterError(False, "Docker Hub returned no bearer token")
        return bearer

    async def call(
        self,
        method: str,
        path: str,
        *,
        json: JsonObj | None = None,
        params: dict[str, str] | None = None,
        accept: frozenset[int] = frozenset(),
    ) -> httpx.Response:
        """Send a request; return on 2xx or a status in `accept`, otherwise raise `AdapterError`."""
        if self._bearer is None:
            self._bearer = await self._login()
        return await self._send(
            method, path, json=json, params=params, accept=accept, bearer=self._bearer
        )


def json_object(response: httpx.Response) -> JsonObj:
    """Parse a JSON object body, or raise a non-retryable `AdapterError`."""
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise AdapterError(False, "Docker Hub returned an unexpected response body")
    return data
