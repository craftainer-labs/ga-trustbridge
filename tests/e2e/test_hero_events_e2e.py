"""E2E: GET .../events (SSE) for Hero against the live api.

Runs against both `app_mode` legs -- `mock` (InMemoryEventSink) and `dev` (the real
MQTT-backed path, see docs/adrs/0015-mqtt-for-crud-events.md) -- the same way every
other test in this directory is parametrized. tests/integration/crud_1/heroes/
test_heroes_v2_events.py already proves the MQTT persistent-session delivery guarantee
in detail; this file's job is narrower: prove the route actually works end to end
against each mode's live process, closing the coverage this suite would otherwise
leave on `craftainer_crud.controllers.crud_router`'s `stream_events`/`_sse_events`,
`craftainer_crud.interfaces.base`'s `MQTTEventSource`/`InMemoryEventSink`, and
`craftainer_crud.interfaces.dependency`'s event-source provider.

Plain (sync) `httpx`, not Playwright's `page.request`, and not `async def` tests: this
endpoint's response body never completes on its own (see the integration test's own
docstring for why a fully-buffering client deadlocks against it), so it needs a real
incremental read -- `page.request`'s APIResponse only ever exposes the fully-buffered
body. A sync `httpx.Client.stream()` reads incrementally over a real socket without
buffering, and without pulling in pytest-asyncio's event loop, which -- sharing this
session with pytest-playwright's own sync-API event loop -- doesn't tolerate an
`async def` test here (verified: mixing the two here reliably breaks event-loop
teardown for every test in the session, async or not).
"""

import json
from collections.abc import Callable

import httpx

_EVENTS_URL = "/crud/v1/heroes/v2/json/events"
_HEROES_URL = "/crud/v1/heroes/v2/json"
_XML_EVENTS_URL = "/crud/v1/heroes/v2/xml/events"


def test_hero_events_stream_delivers_a_create_event(
    base_url: str, access_token: Callable[[str], str]
) -> None:
    """A subscriber connected before a hero is created receives that create event."""
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    with (
        httpx.Client(base_url=base_url, headers=headers, timeout=30) as http,
        http.stream("GET", _EVENTS_URL) as response,
    ):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        lines = response.iter_lines()
        assert next(lines) == "id: 0"
        subscriber_frame = json.loads(next(lines).removeprefix("data: "))
        assert next(lines) == ""  # blank line ending the subscriber_id frame

        created = http.post(
            _HEROES_URL, json={"name": "Streamed Hero", "powers": ["Telepathy"]}
        ).json()
        try:
            assert next(lines) == "id: 1"
            event = json.loads(next(lines).removeprefix("data: "))
            assert event["resource"] == "hero"
            assert event["record_id"] == created["id"]
            assert event["action"] == "create"
        finally:
            http.delete(_HEROES_URL, params={"id": created["id"]})

    assert subscriber_frame["subscriber_id"]


def test_hero_events_stream_sends_keep_alive_when_idle(
    base_url: str, access_token: Callable[[str], str]
) -> None:
    """With no events published, an idle subscriber still gets a keep-alive comment
    (see conftest.py's SSE_KEEPALIVE_SECONDS override for why this doesn't wait 15s).
    """
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    with (
        httpx.Client(base_url=base_url, headers=headers, timeout=30) as http,
        http.stream("GET", _EVENTS_URL) as response,
    ):
        lines = response.iter_lines()
        assert next(lines) == "id: 0"
        next(lines)  # the subscriber_id frame's data line
        next(lines)  # the blank line ending the subscriber_id frame
        assert next(lines) == ": keep-alive"


def test_hero_xml_events_stream_opens(base_url: str, access_token: Callable[[str], str]) -> None:
    """GET .../xml/events opens the same SSE stream as the JSON route -- closes the
    coverage build_xml_router's own stream_events_xml route would otherwise leave on
    crud_router.py (already proven in full, JSON-side, by the tests above).
    """
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    with (
        httpx.Client(base_url=base_url, headers=headers, timeout=30) as http,
        http.stream("GET", _XML_EVENTS_URL) as response,
    ):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        lines = response.iter_lines()
        assert next(lines) == "id: 0"
