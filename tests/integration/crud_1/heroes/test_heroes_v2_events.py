"""Integration test: GET /crud/v1/heroes/v2/json/events against the real Mosquitto stack service.

Unlike tests/unit/controllers/test_crud_router.py's MODE=mock InMemoryEventSink coverage
(best-effort, no delivery guarantee -- see InMemoryEventSink's own docstring), this proves
the actual delivery guarantee docs/adrs/0015-mqtt-for-crud-events.md describes: a
subscriber that disconnects, then reconnects with the same subscriber_id, still receives
an event published while it was offline.

This needs a *genuine* concurrently-streaming HTTP connection -- an in-process ASGI
transport (httpx.ASGITransport, or Starlette's own TestClient) fully drains an ASGI app's
response body before returning anything at all (see httpx._transports.asgi.ASGITransport.
handle_async_request's `await self.app(...)` preceding its `return Response(...)`), which
never happens for this route's stream until the client disconnects -- an in-process
transport therefore deadlocks forever against it, with no way to read even the first
frame while the connection is meant to still be open. A real socket does not have this
problem: bytes flow through the OS as they're written, independent of when the whole
request finishes, and closing the connection is what actually delivers an ASGI
`http.disconnect` message to the running app. `_live_server` below runs the real app
under `uvicorn` on a loopback TCP port, as a task on this test's own event loop (not a
separate thread) so the shared async engine/session singleton (see
craftainer_crud.models.base) stays
on the one event loop tests/README.md's "Do" section requires.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.oidc import get_current_claims

_events_url = "/crud/v1/heroes/v2/json/events"
_heroes_url = "/crud/v1/heroes/v2/json"

client = TestClient(app)


@contextmanager
def _authed_as(sub: str) -> Iterator[None]:
    """Override get_current_claims to authenticate as `sub`, with every RBAC role."""
    settings = get_settings()
    previous = app.dependency_overrides.get(get_current_claims)
    app.dependency_overrides[get_current_claims] = lambda: {
        "sub": sub,
        "resource_access": {
            settings.oidc_client_id: {
                "roles": ["viewer", "editor", "maintainer", "security", "detective"]
            }
        },
    }
    try:
        yield
    finally:
        if previous is None:
            del app.dependency_overrides[get_current_claims]
        else:
            app.dependency_overrides[get_current_claims] = previous


@pytest.fixture(autouse=True)
def _authed() -> Iterator[None]:
    """Authenticate every request in this module as "test-user" by default."""
    with _authed_as("test-user"):
        yield


@asynccontextmanager
async def _live_server() -> AsyncIterator[int]:
    """Serve the real app over a real loopback TCP socket; yield the port it bound.

    `lifespan="off"`: this suite's schema is already migrated by the devcontainer's own
    `postCreateCommand` (see app/README.md's "Alembic migrations"), the same reason every
    other tests/integration case uses a bare TestClient(app) rather than triggering
    lifespan itself.
    """
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        yield server.servers[0].sockets[0].getsockname()[1]
    finally:
        server.should_exit = True
        await task


async def test_hero_events_survive_a_disconnect_with_the_same_subscriber_id() -> None:
    """A hero mutation published while the subscriber is disconnected is still delivered
    once it reconnects with the same subscriber_id -- the delivery guarantee's whole point.
    """

    async with _live_server() as port:
        base_url = f"http://127.0.0.1:{port}"

        # 1. Connect, receive the subscriber_id frame, then disconnect (exiting the
        #    `stream` context closes the real socket, which is what actually delivers
        #    an ASGI http.disconnect to the still-running app).
        async with (
            httpx.AsyncClient(base_url=base_url) as http,
            http.stream("GET", _events_url) as response,
        ):
            assert response.status_code == 200
            lines = response.aiter_lines()
            assert await anext(lines) == "id: 0"
            # The server mints the subscriber id (a client-chosen one is ignored); a client
            # resumes by handing the minted id back.
            subscriber_id = json.loads((await anext(lines)).removeprefix("data: "))["subscriber_id"]

        # Exiting the `async with` above only closes *this* end of the socket --
        # nothing here guarantees the server has noticed yet. Server-side, that
        # happens asynchronously (uvicorn's own read loop has to see the connection
        # close, then cancel this route's task, then MQTTEventSource._events's
        # `finally` has to actually run its detached disconnect task): a client that
        # published the very next moment, with no gap at all, could get there first --
        # the broker would then still see the *old* connection as the live subscriber
        # for `crud-events/hero` and deliver straight to it, not queue for replay,
        # since nobody told the broker this subscriber was gone yet. A real client
        # reconnecting after being "briefly offline" (the scenario this delivery
        # guarantee actually targets -- see docs/adrs/0015-mqtt-for-crud-events.md)
        # doesn't hit this: reconnecting takes at least a network round trip, which is
        # already more than enough time. This sleep stands in for that unavoidable gap.
        await asyncio.sleep(0.5)
        # 2. Mutate a hero while the subscriber above is disconnected.
        created = client.post(
            _heroes_url, json={"name": "Event Guarantee Test", "powers": ["Persistence"]}
        ).json()
        try:
            # 3. Reconnect with the *same* subscriber_id and confirm the missed "create"
            #    event is still delivered -- proving the persistent MQTT session (not
            #    plain SSE, which has no such guarantee on its own) is what holds here.
            async with (
                httpx.AsyncClient(base_url=base_url) as http,
                http.stream(
                    "GET", _events_url, params={"subscriber_id": subscriber_id}
                ) as response,
            ):
                assert response.status_code == 200
                lines = response.aiter_lines()
                assert await anext(lines) == "id: 0"  # the subscriber_id frame's `id:` line
                await anext(lines)  # the subscriber_id frame's `data:` line
                await anext(lines)  # the blank line ending the subscriber_id frame
                assert await anext(lines) == "id: 1"  # the missed event's `id:` line
                event = json.loads((await anext(lines)).removeprefix("data: "))
                assert event["resource"] == "hero"
                assert event["record_id"] == created["id"]
                assert event["action"] == "create"
        finally:
            client.delete(_heroes_url, params={"id": created["id"]})
