"""Replay recorded-style Kubernetes API exchanges through an `httpx.MockTransport`.

A fixture file holds named scenarios; a scenario is an ordered list of request/response pairs.
The transport asserts that the adapter sends exactly those requests in that order (method, path,
query, content type and JSON body when the fixture gives them, bearer auth) and answers with the
recorded response. No network is involved. `${name}` placeholders stand for values derived from
the tenant and scope.
"""

import json
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).parent / "fixtures"
SA_TOKEN = "service-account-token-value"  # noqa: S105 - a dummy used only in these fixtures
BASE_URL = "https://kube.example:6443"


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
        assert request.url.path == want["path"]
        assert dict(request.url.params) == want.get("query", {})
        assert request.headers["authorization"] == f"Bearer {SA_TOKEN}"
        assert str(request.url).startswith(BASE_URL)
        if "content_type" in want:
            assert request.headers["content-type"] == want["content_type"]
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
