"""GitHub's hook-delivery endpoints: listing with cursor pagination and redelivery."""

from datetime import UTC, datetime

import httpx2
import pytest

from tests.trustbridge.github.conftest import (
    Env,
    FakeChecker,
    KeySigner,
    ScriptedGitHub,
    make_host_config,
)
from trustbridge.github.availability import HostMonitor, HostNotReadyError, MonitorDeps
from trustbridge.github.client import GitHubApi, GitHubRequester
from trustbridge.github.hook_deliveries import GitHubHookDeliveries, next_cursor
from trustbridge.github.hosts import GitHubHostConfig

PATH = "/app/hook/deliveries"


def _item(
    hook_id: int, guid: str, status: int, delivered_at: str, *, redelivery: bool = False
) -> dict[str, object]:
    return {
        "id": hook_id,
        "guid": guid,
        "status_code": status,
        "delivered_at": delivered_at,
        "redelivery": redelivery,
    }


def _client(env: Env, gh: ScriptedGitHub) -> GitHubHookDeliveries:
    def factory(host: GitHubHostConfig) -> GitHubRequester:
        return GitHubApi(host, async_transport=gh.transport())

    return GitHubHookDeliveries(env.monitor, {"main": env.signer}, factory)


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        (None, None),
        ('<https://api.github.com/app/hook/deliveries?per_page=100>; rel="prev"', None),
        ('<https://api.github.com/app/hook/deliveries?per_page=100>; rel="next"', None),
        ('<https://api.github.com/app/hook/deliveries?cursor=v1_9>; rel="next"', "v1_9"),
    ],
)
def test_next_cursor_reads_the_next_relation_only(link: str | None, expected: str | None) -> None:
    """Next cursor reads the next relation only."""
    assert next_cursor(link) == expected


async def test_deliveries_are_listed_across_pages_until_the_cutoff(
    env: Env, gh: ScriptedGitHub
) -> None:
    """Deliveries are listed across pages until the cutoff."""
    pages = [
        (
            [
                _item(3, "g3", 502, "2026-11-01T12:00:00Z", redelivery=True),
                _item(2, "g2", 200, "2026-11-01T11:00:00Z"),
            ],
            '<https://api.github.com/app/hook/deliveries?per_page=100&cursor=v1_2>; rel="next"',
        ),
        ([_item(1, "g1", 500, "2026-10-01T00:00:00Z")], None),
    ]
    seen_cursors: list[str | None] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        seen_cursors.append(request.url.params.get("cursor"))
        assert request.headers["Authorization"].startswith("Bearer ")
        body, link = pages[len(seen_cursors) - 1]
        return httpx2.Response(200, json=body, headers={"link": link} if link else {})

    gh.on("GET", PATH, respond)
    since = datetime(2026, 10, 31, tzinfo=UTC)
    got = [d async for d in _client(env, gh).list_deliveries(env.host, since=since)]
    assert [(d.id, d.guid, d.status_code, d.redelivery) for d in got] == [
        (3, "g3", 502, True),
        (2, "g2", 200, False),
    ]
    assert seen_cursors == [None, "v1_2"]  # the second page was fetched, then the cutoff ended it


async def test_listing_ends_when_a_page_has_no_next_relation_or_no_body(
    env: Env, gh: ScriptedGitHub
) -> None:
    """Listing ends when a page has no next relation or no body."""
    gh.route("GET", PATH, body=None)
    since = datetime(2026, 10, 31, tzinfo=UTC)
    assert [d async for d in _client(env, gh).list_deliveries(env.host, since=since)] == []
    gh.route("GET", PATH, body=[_item(1, "g1", 200, "2026-11-01T00:00:00Z")])
    assert len([d async for d in _client(env, gh).list_deliveries(env.host, since=since)]) == 1


async def test_a_delivery_is_redelivered_by_its_numeric_id(env: Env, gh: ScriptedGitHub) -> None:
    """A delivery is redelivered by its numeric id."""
    gh.route("POST", f"{PATH}/77/attempts", 202, {})
    await _client(env, gh).redeliver(env.host, 77)
    assert gh.paths() == [f"POST {PATH}/77/attempts"]


async def test_a_host_that_is_not_ready_is_never_called(gh: ScriptedGitHub) -> None:
    """A host that is not ready is never called."""
    host = make_host_config()
    signer = KeySigner()
    monitor = HostMonitor([host], {"main": signer}, MonitorDeps(checker=FakeChecker(app_id=1)))
    await monitor.check_all()  # the wrong App id: unavailable
    client = GitHubHookDeliveries(monitor, {"main": signer})
    with pytest.raises(HostNotReadyError):
        await client.redeliver(host, 1)
    with pytest.raises(HostNotReadyError):
        _ = [d async for d in client.list_deliveries(host, since=datetime.now(UTC))]
    assert gh.requests == []
