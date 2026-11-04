"""A thin httpx client for the Harbor REST API v2.0 the adapter uses (no business rules)."""

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


def seg(value: str) -> str:
    """Percent-encode one URL path segment (names never smuggle a `/` or `..`)."""
    return quote(value, safe="")


def retryable_status(status: int) -> bool:
    """Return whether an HTTP status is worth retrying."""
    return status in {HTTP_REQUEST_TIMEOUT, HTTP_TOO_EARLY, HTTP_TOO_MANY_REQUESTS} or (
        status >= HTTP_SERVER_ERROR
    )


class HarborClient:
    """One short-lived HTTP session against a Harbor URL, authenticated with HTTP basic auth.

    Error messages carry the method, the path and the status, never a body or a header, so no
    secret can reach an `AdapterError` (invariant A5).
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: SecretStr,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """Build the session; nothing is sent until `call`."""
        # Numeric project ids resolved during this session: one operation, one lookup per project
        # (None records a 404).
        self.project_ids: dict[str, int | None] = {}
        self._base = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            auth=httpx.BasicAuth(username, password.get_secret_value()),
            headers={"Accept": "application/json"},
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
        )

    async def __aenter__(self) -> HarborClient:
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
        try:
            response = await self._http.request(
                method, f"{self._base}{path}", json=json, params=params
            )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            msg = f"Harbor {method} {path} failed: {type(exc).__name__}"
            raise AdapterError(True, msg) from None
        status = response.status_code
        if response.is_success or status in accept:
            return response
        msg = f"Harbor {method} {path} failed with HTTP {status}"
        raise AdapterError(retryable_status(status), msg)


def json_object(response: httpx.Response) -> JsonObj:
    """Parse a JSON object body, or raise a non-retryable `AdapterError`."""
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise AdapterError(False, "Harbor returned an unexpected response body")
    return data


def json_list(response: httpx.Response) -> list[JsonObj]:
    """Parse a JSON array of objects, or raise a non-retryable `AdapterError`."""
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise AdapterError(False, "Harbor returned an unexpected listing body")
    return data
