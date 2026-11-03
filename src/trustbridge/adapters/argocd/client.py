"""A thin httpx client for the Kubernetes API calls the adapter makes (no business rules)."""

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
MERGE_PATCH = "application/merge-patch+json"


def seg(value: str) -> str:
    """Percent-encode one URL path segment (names never smuggle a `/` or `..`)."""
    return quote(value, safe="")


def retryable_status(status: int) -> bool:
    """Return whether an HTTP status is worth retrying."""
    return status in {HTTP_REQUEST_TIMEOUT, HTTP_TOO_EARLY, HTTP_TOO_MANY_REQUESTS} or (
        status >= HTTP_SERVER_ERROR
    )


class KubeClient:
    """One short-lived HTTP session against the Argo CD cluster's API server (ServiceAccount token).

    Error messages carry the method, the path and the status, never a body or a header, so no
    secret can reach an `AdapterError` (invariant A5).
    """

    def __init__(
        self,
        base_url: str,
        token: SecretStr,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """Build the session; nothing is sent until `call`."""
        self._base = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {token.get_secret_value()}",
                "Accept": "application/json",
            },
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
        )

    async def __aenter__(self) -> KubeClient:
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
        """Send a request (a PATCH is a JSON merge patch); return on 2xx or `accept`, else raise."""
        headers = {"Content-Type": MERGE_PATCH} if method == "PATCH" else None
        try:
            response = await self._http.request(
                method, f"{self._base}{path}", json=json, params=params, headers=headers
            )
        except httpx.HTTPError as exc:
            msg = f"Argo CD cluster {method} {path} failed: {type(exc).__name__}"
            raise AdapterError(True, msg) from None
        status = response.status_code
        if response.is_success or status in accept:
            return response
        msg = f"Argo CD cluster {method} {path} failed with HTTP {status}"
        raise AdapterError(retryable_status(status), msg)


def json_object(response: httpx.Response) -> JsonObj:
    """Parse a JSON object body, or raise a non-retryable `AdapterError`."""
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise AdapterError(False, "the Argo CD cluster returned an unexpected response body")
    return data
