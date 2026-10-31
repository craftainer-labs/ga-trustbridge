"""E2E: ETag / 304 / If-Match / 412 on the hero JSON and XML routes against the live api."""

from collections.abc import Callable, Iterator
from uuid import uuid4

import pytest
from playwright.sync_api import APIRequestContext, Page

_REVALIDATE = "private, no-cache"


def _xml_id(body: str) -> str:
    return body.split("<id>")[1].split("</id>", maxsplit=1)[0]


class _Journey:
    """One format's route URL, request headers and body builders."""

    def __init__(
        self, request: APIRequestContext, url: str, headers: dict[str, str], xml: bool
    ) -> None:
        self.request = request
        self.url = url
        self.headers = headers
        self.xml = xml

    def create(self, name: str) -> str:
        data = (
            f"<hero><name>{name}</name><powers>Cache</powers></hero>"
            if self.xml
            else {"name": name, "powers": ["Cache"]}
        )
        response = self.request.post(self.url, data=data, headers=self.headers)
        assert response.status == 201
        return _xml_id(response.text()) if self.xml else str(response.json()["id"])

    def patch_body(self, power: str) -> str | dict[str, list[str]]:
        return f"<hero><powers>{power}</powers></hero>" if self.xml else {"powers": [power]}


@pytest.fixture(params=["json", "xml"])
def journey(
    request: pytest.FixtureRequest,
    page: Page,
    base_url: str,
    access_token: Callable[[str], str],
) -> Iterator[_Journey]:
    """A journey bound to the JSON or the XML route, deleting the heroes it created."""
    xml = request.param == "xml"
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    if xml:
        headers["Content-Type"] = "application/xml"
    journey = _Journey(page.request, f"{base_url}/crud/v1/heroes/v2/{request.param}", headers, xml)
    yield journey
    # The heroes themselves are removed inside each test; nothing else to clean up.


def test_conditional_get(journey: _Journey) -> None:
    """ETag/Cache-Control on single and list GETs, and 304 on revalidation."""
    request, url, headers = journey.request, journey.url, journey.headers
    hero_id = journey.create(f"Etag {uuid4().hex[:8]}")
    try:
        first = request.get(url, params={"id": hero_id}, headers=headers)
        assert first.ok
        etag = first.headers["etag"]
        assert etag.startswith('"')
        assert first.headers["cache-control"] == _REVALIDATE

        revalidated = request.get(
            url, params={"id": hero_id}, headers={**headers, "If-None-Match": etag}
        )
        assert revalidated.status == 304
        assert revalidated.body() == b""
        assert revalidated.headers["etag"] == etag
        assert revalidated.headers["cache-control"] == _REVALIDATE

        listing = request.get(url, headers=headers)
        assert listing.ok
        list_etag = listing.headers["etag"]
        assert list_etag.startswith('W/"')
        assert listing.headers["cache-control"] == _REVALIDATE
        list_again = request.get(url, headers={**headers, "If-None-Match": list_etag})
        assert list_again.status == 304
        assert list_again.body() == b""

        # A non-matching tag gets the full response.
        stale_get = request.get(
            url, params={"id": hero_id}, headers={**headers, "If-None-Match": '"nope"'}
        )
        assert stale_get.status == 200
    finally:
        request.delete(url, params={"id": hero_id}, headers=headers)


def test_if_match_preconditions(journey: _Journey) -> None:
    """If-Match: success with the current tag, 412 problem details when stale."""
    request, url, headers = journey.request, journey.url, journey.headers
    name = f"Etag {uuid4().hex[:8]}"
    hero_id = journey.create(name)
    other_id = journey.create(name)
    try:
        etag = request.get(url, params={"id": hero_id}, headers=headers).headers["etag"]
        # Current If-Match: the PATCH applies and returns a new ETag.
        updated = request.patch(
            url,
            params={"id": hero_id},
            data=journey.patch_body("Revalidation"),
            headers={**headers, "If-Match": etag},
        )
        assert updated.status == 200
        new_etag = updated.headers["etag"]
        assert new_etag != etag
        refetched = request.get(url, params={"id": hero_id}, headers=headers)
        assert refetched.headers["etag"] == new_etag

        # The old tag is now stale: PATCH and DELETE both 412 with problem details.
        stale_patch = request.patch(
            url,
            params={"id": hero_id},
            data=journey.patch_body("Lost update"),
            headers={**headers, "If-Match": etag},
        )
        assert stale_patch.status == 412
        assert stale_patch.headers["content-type"].startswith("application/problem+json")
        assert stale_patch.json()["status"] == 412
        stale_delete = request.delete(
            url, params={"id": hero_id}, headers={**headers, "If-Match": etag}
        )
        assert stale_delete.status == 412
        assert stale_delete.headers["content-type"].startswith("application/problem+json")

        # `*` matches any existing record.
        star = request.patch(
            url,
            params={"id": hero_id},
            data=journey.patch_body("Star"),
            headers={**headers, "If-Match": "*"},
        )
        assert star.status == 200

        # A bulk action (filters, no id) ignores If-Match.
        bulk = request.patch(
            url,
            params={"name": name},
            data=journey.patch_body("Bulk"),
            headers={**headers, "If-Match": '"stale"'},
        )
        assert bulk.status == 200
        assert "etag" not in bulk.headers

        # DELETE with the record's current tag succeeds; the record is then gone, so a
        # conditional write on it falls through to 404 rather than 412.
        current = request.get(url, params={"id": other_id}, headers=headers).headers["etag"]
        deleted = request.delete(
            url, params={"id": other_id}, headers={**headers, "If-Match": current}
        )
        assert deleted.status == 204
        gone = request.patch(
            url,
            params={"id": other_id},
            data=journey.patch_body("Gone"),
            headers={**headers, "If-Match": current},
        )
        assert gone.status == 404
    finally:
        for target in (hero_id, other_id):
            request.delete(url, params={"id": target}, headers=headers)
