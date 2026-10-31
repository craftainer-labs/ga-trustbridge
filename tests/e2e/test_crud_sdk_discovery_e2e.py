"""E2E: discover the manifest through /.well-known/api-catalog with the Python SDK, then read.

Drives the live api with `craftainer_crud_sdk` (sync and async) the way a generated client
would: nothing is hardcoded but the origin, the manifest URL comes from the api-catalog and
every resource path from the manifest.
"""

import asyncio
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest
from craftainer_crud_sdk import (
    AsyncCrudClient,
    CrudClient,
    Resource,
    UnknownResourceError,
    static_token,
)


@pytest.fixture
def seeded_hero(base_url: str, access_token: Callable[[str], str]) -> Iterator[dict[str, object]]:
    """Create a hero as maintainer over plain HTTP and delete it afterwards."""
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    url = f"{base_url}/crud/v1/heroes/v2/json"
    created = httpx.post(
        url,
        json={"name": f"Sdk {uuid4().hex[:8]}", "powers": ["Discovery"]},
        headers=headers,
        timeout=10,
    )
    assert created.status_code == 201
    hero: dict[str, object] = created.json()
    try:
        yield hero
    finally:
        httpx.delete(url, params={"id": str(hero["id"])}, headers=headers, timeout=10)


def _assert_heroes_v2(resource: Resource) -> None:
    assert resource.api_version == "v2"
    assert resource.base_path.endswith("/heroes/v2/json")
    assert resource.auth.read_roles == ("viewer", "editor", "maintainer", "detective")
    assert resource.capabilities.events is not None


def test_catalog_then_manifest_over_http(base_url: str, access_token: Callable[[str], str]) -> None:
    """The catalog names the manifest; the manifest honours ETag and the caller's roles."""
    catalog = httpx.get(f"{base_url}/.well-known/api-catalog", timeout=10)
    assert catalog.status_code == 200
    assert catalog.headers["content-type"].startswith("application/linkset+json")
    link = catalog.json()["linkset"][0]["describedby"][0]
    assert link["href"] == "/api/_crud/manifest"

    anonymous = httpx.get(f"{base_url}{link['href']}", timeout=10)
    assert anonymous.status_code == 200
    assert anonymous.json()["resources"] == []

    headers = {"Authorization": f"Bearer {access_token('viewer')}"}
    first = httpx.get(f"{base_url}{link['href']}", headers=headers, timeout=10)
    assert {r["api_version"] for r in first.json()["resources"]} == {"v1", "v2"}
    assert first.headers["cache-control"] == "private, no-cache"
    again = httpx.get(
        f"{base_url}{link['href']}",
        headers={**headers, "If-None-Match": first.headers["etag"]},
        timeout=10,
    )
    assert again.status_code == 304


def test_sdk_discovers_and_reads_sync(
    base_url: str, access_token: Callable[[str], str], seeded_hero: dict[str, object]
) -> None:
    """The sync client discovers via the catalog, then gets and lists the seeded hero."""
    token = access_token("viewer")
    with CrudClient(base_url, token_provider=static_token(token)) as client:
        manifest = client.manifest()
        assert manifest.manifest_version == "1"
        heroes = client.resource("heroes")
        _assert_heroes_v2(manifest.find("heroes", "v2") or pytest.fail("no heroes v2"))

        assert heroes.get(int(str(seeded_hero["id"])))["name"] == seeded_hero["name"]
        listed = heroes.list({"name": str(seeded_hero["name"])})
        assert [hero["id"] for hero in listed] == [seeded_hero["id"]]
        assert any(hero["id"] == seeded_hero["id"] for hero in heroes.iter_all(sort="name"))


async def _read_async(base_url: str, token: str, hero_id: int, name: str) -> None:
    async with AsyncCrudClient(base_url, token_provider=static_token(token)) as client:
        manifest = await client.manifest()
        _assert_heroes_v2(manifest.find("heroes", "v2") or pytest.fail("no heroes v2"))
        heroes = await client.resource("heroes")

        assert (await heroes.get(hero_id))["name"] == name
        listed = await heroes.list({"name": name})
        assert [hero["id"] for hero in listed] == [hero_id]


def test_sdk_discovers_and_reads_async(
    base_url: str, access_token: Callable[[str], str], seeded_hero: dict[str, object]
) -> None:
    """The async client does the same.

    Run on a loop of its own in a worker thread: the session's Playwright fixtures keep the
    main thread's event loop busy, which a plain `async def` test (or `asyncio.run` there)
    would collide with.
    """
    arguments = (
        base_url,
        access_token("viewer"),
        int(str(seeded_hero["id"])),
        str(seeded_hero["name"]),
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, _read_async(*arguments)).result(timeout=60)


def test_sdk_anonymous_sees_no_resource(base_url: str) -> None:
    """Without a token the manifest is empty, so asking for heroes is an SDK error."""
    with CrudClient(base_url) as client:
        assert client.manifest().resources == ()
        with pytest.raises(UnknownResourceError):
            client.resource("heroes")
