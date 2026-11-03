"""Replay recorded-style Docker Hub exchanges through an `httpx.MockTransport`.

A fixture file holds named scenarios; a scenario is an ordered list of request/response pairs.
The transport asserts that the adapter sends exactly those requests in that order (method, path
with query, JSON body when the fixture gives one, and the bearer from the login exchange) and
answers with the recorded response. No network is involved. `${name}` placeholders stand for
values derived from the tenant and scope.
"""

import json
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).parent / "fixtures"
PAT = "dckr_pat_dummy"
BEARER = "jwt-bearer-value"
BASE_URL = "https://hub.docker.com"
LOGIN_PATH = "/v2/auth/token"


def load_scenario(file: str, name: str, context: dict[str, str]) -> list[dict[str, Any]]:
    """Return the exchanges of one scenario with placeholders substituted."""
    raw = (FIXTURES / file).read_text()
    for key, value in context.items():
        raw = raw.replace("${" + key + "}", value)
    scenarios: dict[str, Any] = json.loads(raw)["scenarios"]
    exchanges: list[dict[str, Any]] = scenarios[name]["exchanges"]
    return exchanges


class Replay:
    """A strict, ordered replay of recorded exchanges."""

    def __init__(self, exchanges: list[dict[str, Any]]) -> None:
        """Hold the expected exchanges."""
        self.exchanges = exchanges
        self.position = 0
        self.seen: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        """Return the transport to hand to the adapter."""
        return httpx.MockTransport(self._handle)

    def assert_exhausted(self) -> None:
        """Fail when the adapter sent fewer requests than recorded."""
        assert self.position == len(self.exchanges), "recorded exchanges left unused"

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        assert self.position < len(self.exchanges), f"unexpected {request.method} {request.url}"
        expected = self.exchanges[self.position]
        self.position += 1
        want = expected["request"]
        assert request.method == want["method"]
        assert request.url.raw_path.decode() == want["path"]
        if request.url.path == LOGIN_PATH:
            assert "authorization" not in request.headers
        else:
            assert request.headers["authorization"] == f"Bearer {BEARER}"
        assert str(request.url).startswith(BASE_URL)
        if "json" in want:
            assert json.loads(request.content) == want["json"]
        else:
            assert not request.content
        reply = expected["response"]
        if "raise" in reply:
            raise getattr(httpx, reply["raise"])("recorded transport failure", request=request)
        if "text" in reply:
            return httpx.Response(reply["status"], text=reply["text"])
        return httpx.Response(reply["status"], json=reply.get("json"))
