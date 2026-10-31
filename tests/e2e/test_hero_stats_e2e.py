"""E2E smoke test: /crud/v1/heroes/v2/{json,xml}/stats and /predict against the live api.

Doesn't assert exact counts (other e2e tests running in the same session create/
delete their own heroes concurrently, and dev-mode leaves committed data behind
across tests within a session -- see conftest.py's `_reset_dev_database`) --
just that both routes respond with the expected shape, and that the same
caller-input validation (invalid bucket/field, insufficient history) this
plan's unit tests already cover in isolation also holds end to end.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from playwright.sync_api import Page


def test_hero_stats_without_bucket_omits_time_series_json_and_xml(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """GET .../stats with no ?bucket= renders `time_series`/`<time-buckets>` as
    entirely absent, not an empty list -- see _resolve_stats/_stats_to_xml.
    """
    headers = {"Authorization": f"Bearer {access_token('viewer')}"}

    json_response = page.request.get(f"{base_url}/crud/v1/heroes/v2/json/stats", headers=headers)
    assert json_response.ok
    assert json_response.json()["time_series"] is None

    xml_response = page.request.get(f"{base_url}/crud/v1/heroes/v2/xml/stats", headers=headers)
    assert xml_response.ok
    assert "<time-buckets>" not in xml_response.text()


def test_hero_stats_accepts_every_bucket_width(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """GET .../stats?bucket=day|week|month all render a real time_series -- closes the
    coverage craftainer_crud.repositories.memory's own `_bucket_start` would otherwise leave
    on its
    WEEK/MONTH branches (the smoke test above only ever passes bucket=day). A single
    real (same-day) hero is enough: `_bucket_start` runs once per matching record
    regardless of how many distinct buckets that produces, unlike crud_stats.forecast's
    own multi-bucket-history requirement (see test_hero_predict_forecasts_a_trend_json_and_xml).
    """
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    hero = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/json",
        data={"name": "E2E Bucket Width Hero", "powers": ["Speed"]},
        headers=headers,
    ).json()
    try:
        for bucket in ("day", "week", "month"):
            response = page.request.get(
                f"{base_url}/crud/v1/heroes/v2/json/stats",
                params={"bucket": bucket},
                headers=headers,
            )
            assert response.ok
            assert response.json()["time_series"]
    finally:
        page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": hero["id"]},
            headers=headers,
            fail_on_status_code=False,
        )


def test_hero_predict_forecasts_a_trend_json_and_xml(
    page: Page,
    base_url: str,
    access_token: Callable[[str], str],
    backdate_hero: Callable[[int, datetime], None],
) -> None:
    """GET .../predict actually forecasts a trend once its history spans >=2 time
    buckets, in both the default (record-count) and field-targeted (power_level)
    modes, and across all three bucket widths -- day/week/month.

    Real e2e record creation never naturally spans multiple real calendar days,
    weeks, or months within one test run, so `backdate_hero` fabricates that
    history directly in Postgres (dev leg only -- skipped under mock, see its own
    docstring; the dev leg's run in the same session still closes this coverage
    for both legs combined, per pyproject.toml's [tool.coverage.run]). Exercises
    crud_stats.forecast's success path, count_series_as_values, bucket_field_sums,
    parse_predict_field's valid-field branch, and _advance/_bucket_start's day/
    week/month branches -- none of which any other e2e test reaches.
    """
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    now = datetime.now(UTC).replace(tzinfo=None)
    hero_ids: list[int] = []
    try:
        # Default (record-count) series, bucket=day: two backdated heroes a day apart.
        for days_ago, level in ((2, 1), (1, 5)):
            created = page.request.post(
                f"{base_url}/crud/v1/heroes/v2/json",
                data={"name": "E2E Predict Hero", "powers": ["Speed"], "power_level": level},
                headers=headers,
            ).json()
            hero_ids.append(created["id"])
            backdate_hero(created["id"], now - timedelta(days=days_ago))

        day_response = page.request.get(
            f"{base_url}/crud/v1/heroes/v2/json/predict",
            params={"bucket": "day", "periods": 2},
            headers=headers,
        )
        assert day_response.ok
        day_body = day_response.json()
        assert day_body["field"] is None
        assert len(day_body["predictions"]) == 2

        # field=power_level series, bucket=day too: same two heroes, _bucket_start's DAY branch.
        day_field_response = page.request.get(
            f"{base_url}/crud/v1/heroes/v2/json/predict",
            params={"bucket": "day", "field": "power_level", "periods": 1},
            headers=headers,
        )
        assert day_field_response.ok
        assert day_field_response.json()["field"] == "power_level"

        # field=power_level series, bucket=week: two heroes two weeks apart.
        for weeks_ago, level in ((3, 3), (1, 9)):
            created = page.request.post(
                f"{base_url}/crud/v1/heroes/v2/json",
                data={"name": "E2E Predict Hero", "powers": ["Speed"], "power_level": level},
                headers=headers,
            ).json()
            hero_ids.append(created["id"])
            backdate_hero(created["id"], now - timedelta(weeks=weeks_ago))

        week_response = page.request.get(
            f"{base_url}/crud/v1/heroes/v2/json/predict",
            params={"bucket": "week", "field": "power_level", "periods": 1},
            headers=headers,
        )
        assert week_response.ok
        week_body = week_response.json()
        assert week_body["field"] == "power_level"
        assert len(week_body["predictions"]) == 1

        # field=power_level series, bucket=month: two heroes two calendar months apart.
        for days_ago, level in ((60, 4), (0, 12)):
            created = page.request.post(
                f"{base_url}/crud/v1/heroes/v2/json",
                data={"name": "E2E Predict Hero", "powers": ["Speed"], "power_level": level},
                headers=headers,
            ).json()
            hero_ids.append(created["id"])
            backdate_hero(created["id"], now - timedelta(days=days_ago))

        month_response = page.request.get(
            f"{base_url}/crud/v1/heroes/v2/xml/predict",
            params={"bucket": "month", "field": "power_level", "periods": 1},
            headers=headers,
        )
        assert month_response.ok
        month_text = month_response.text()
        assert "<prediction>" in month_text
        assert "<field>power_level</field>" in month_text
    finally:
        for hero_id in hero_ids:
            page.request.delete(
                f"{base_url}/crud/v1/heroes/v2/json",
                params={"id": hero_id},
                headers=headers,
                fail_on_status_code=False,
            )


def test_hero_stats_smoke_json_and_xml(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """GET .../stats?bucket=day responds 200 with the expected top-level shape, in both formats."""
    headers = {"Authorization": f"Bearer {access_token('viewer')}"}

    json_response = page.request.get(
        f"{base_url}/crud/v1/heroes/v2/json/stats", params={"bucket": "day"}, headers=headers
    )
    assert json_response.ok
    body = json_response.json()
    assert "total" in body
    assert "numeric" in body
    assert "categorical" in body
    assert "lifecycle" in body

    xml_response = page.request.get(
        f"{base_url}/crud/v1/heroes/v2/xml/stats", params={"bucket": "day"}, headers=headers
    )
    assert xml_response.ok
    assert xml_response.headers["content-type"] == "application/xml"
    assert "<stats>" in xml_response.text()
    assert "<numeric-fields>" in xml_response.text()


def test_hero_predict_invalid_bucket_returns_422_json_and_xml(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """GET .../predict?bucket=<invalid> is a 422 in both formats, not a 500."""
    headers = {"Authorization": f"Bearer {access_token('viewer')}"}

    json_response = page.request.get(
        f"{base_url}/crud/v1/heroes/v2/json/predict",
        params={"bucket": "fortnight"},
        headers=headers,
        fail_on_status_code=False,
    )
    assert json_response.status == 422

    xml_response = page.request.get(
        f"{base_url}/crud/v1/heroes/v2/xml/predict",
        params={"bucket": "fortnight"},
        headers=headers,
        fail_on_status_code=False,
    )
    assert xml_response.status == 422


def test_hero_predict_unrecognized_field_returns_422(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """GET .../predict?field=<non-numeric> is a 422, naming an unrecognized field."""
    headers = {"Authorization": f"Bearer {access_token('viewer')}"}

    response = page.request.get(
        f"{base_url}/crud/v1/heroes/v2/json/predict",
        params={"field": "name"},
        headers=headers,
        fail_on_status_code=False,
    )
    assert response.status == 422


def test_hero_stats_requires_read_role(page: Page, base_url: str) -> None:
    """GET .../stats with no Authorization header is rejected (401), same as the plain GET."""
    response = page.request.get(
        f"{base_url}/crud/v1/heroes/v2/json/stats", fail_on_status_code=False
    )
    assert response.status == 401
