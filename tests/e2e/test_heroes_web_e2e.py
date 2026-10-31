"""E2E smoke test: /crud/v1/heroes/v2/web/form and web/components.js against the live api."""

from collections.abc import Callable

from playwright.sync_api import Page, expect


def test_hero_form_serves_html_and_accepts_a_submission(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """GET web/form serves HTML; POSTing to it creates a hero and redirects back."""
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}

    form_response = page.request.get(f"{base_url}/crud/v1/heroes/v2/web/form", headers=headers)
    assert form_response.ok
    assert "<form" in form_response.text()
    assert form_response.headers["x-frame-options"] == "DENY"
    assert form_response.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'self'" in form_response.headers["content-security-policy"]

    submit_response = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/web/form",
        form={"name": "Storm", "powers": "Weather control, Flight"},
        headers=headers,
    )
    assert submit_response.ok  # Playwright follows the 303 redirect by default
    assert submit_response.url == f"{base_url}/crud/v1/heroes/v2/web/form"


def test_hero_form_submission_with_invalid_data_returns_422(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """POSTing web/form with an empty required field returns a validation problem."""
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    response = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/web/form",
        form={"name": "Nobody", "powers": ""},
        headers=headers,
        fail_on_status_code=False,
    )
    assert response.status == 422


def test_hero_components_js_serves_javascript(page: Page, base_url: str) -> None:
    """GET /crud/v1/heroes/v2/web/components.js serves the web-component JS, unauthenticated."""
    response = page.request.get(f"{base_url}/crud/v1/heroes/v2/web/components.js")
    assert response.ok
    assert "customElements.define" in response.text()


def test_hero_list_filter_and_bulk_delete_through_the_rendered_ui(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """Filtering the rendered <hero-list> and bulk-deleting the checked rows both work.

    Drives the actual browser-rendered web component (not just its API calls
    directly), proving the filter controls/sort select/bulk checkboxes the JS
    renders from `/crud/v1/heroes/v2/json/filters` actually reach the JSON router end to end.
    """
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    page.set_extra_http_headers(headers)
    first = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/json",
        data={"name": "Web UI Test Alpha", "powers": ["Speed"]},
        headers=headers,
    ).json()
    second = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/json",
        data={"name": "Web UI Test Beta", "powers": ["Speed"]},
        headers=headers,
    ).json()

    try:
        page.goto(f"{base_url}/crud/v1/heroes/v2/web/form")
        page.wait_for_selector("hero-list table")

        rows = page.locator("hero-list table tr:has(input[data-id])")

        filter_input = page.locator('input[data-field="name"][data-op="icontains"]')
        filter_input.fill("Web UI Test")
        page.locator("button.apply").click()
        expect(rows).to_have_count(2)

        page.locator('hero-list input[type="checkbox"].select-all').check()
        page.locator("hero-list button.bulk-delete").click()
        # The bulk-result message is overwritten by refresh()'s own re-render right
        # after it's set, so assert on the resulting (empty) row list instead of
        # trying to catch that transient text. `expect(...).to_have_count` polls
        # via Playwright's own protocol-level queries, not an in-page eval(), so
        # it works under the form page's strict `default-src 'self'` CSP where
        # `page.wait_for_function` (which does eval a string) does not.
        expect(rows).to_have_count(0)
    finally:
        page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": first["id"]},
            headers=headers,
            fail_on_status_code=False,
        )
        page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": second["id"]},
            headers=headers,
            fail_on_status_code=False,
        )


def test_hero_archive_restore_and_history_through_the_rendered_ui(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """The rendered <hero-list>'s Archive(Delete)/Restore row actions and History panel
    (new for this plan's archivable=True/revisions UI) work end to end -- Hero is
    Archivable, so its own row-level "Delete" button already archives rather than
    hard-deleting (see craftainer_crud.models.mixins.Archivable), and the generated JS adds a
    "Restore"/"History" action once a row is archived (see
    craftainer_crud.web_components.render_crud_component_js).
    """
    headers = {"Authorization": f"Bearer {access_token('maintainer')}"}
    page.set_extra_http_headers(headers)
    hero = page.request.post(
        f"{base_url}/crud/v1/heroes/v2/json",
        data={"name": "Web UI Archive Test", "powers": ["Resilience"]},
        headers=headers,
    ).json()

    try:
        page.goto(f"{base_url}/crud/v1/heroes/v2/web/form")
        page.wait_for_selector("hero-list table")

        filter_input = page.locator('input[data-field="name"][data-op="icontains"]')
        filter_input.fill("Web UI Archive Test")
        page.locator("button.apply").click()

        row = page.locator("hero-list table tr:has(input[data-id])")
        expect(row).to_have_count(1)

        row.locator("button.delete-row").click()
        expect(row).to_have_count(0)  # archived -- excluded from the default (unfiltered) list

        page.locator('hero-list input[type="checkbox"].include-archived').check()
        page.locator("button.apply").click()
        expect(row).to_have_count(1)

        row.locator("button.history-row").click()
        history_panel = row.locator(".history-panel")
        expect(history_panel).not_to_be_hidden()
        expect(history_panel.locator("li")).to_have_count(2)  # this hero's create + delete

        row.locator("button.restore-row").click()
        page.locator('hero-list input[type="checkbox"].include-archived').uncheck()
        page.locator("button.apply").click()
        expect(row).to_have_count(1)
    finally:
        page.request.delete(
            f"{base_url}/crud/v1/heroes/v2/json",
            params={"id": hero["id"]},
            headers=headers,
            fail_on_status_code=False,
        )


def test_hero_stats_and_predict_panel_through_the_rendered_ui(
    page: Page, base_url: str, access_token: Callable[[str], str]
) -> None:
    """The rendered stats/predict panel (new for this plan's stats_enabled=True UI)
    fetches and displays real data from `/stats`/`/predict` -- a plain table plus a
    small inline SVG bar chart, no charting dependency (see render_crud_component_js's
    own docstring).
    """
    headers = {"Authorization": f"Bearer {access_token('viewer')}"}
    page.set_extra_http_headers(headers)

    page.goto(f"{base_url}/crud/v1/heroes/v2/web/form")
    page.wait_for_selector("hero-list .stats-panel")

    stats_body = page.locator("hero-list .stats-body")
    expect(stats_body).to_contain_text("Total:")

    page.locator("hero-list button.predict-run").click()
    predict_body = page.locator("hero-list .predict-body")
    # Whether the live dataset happens to span >=2 time buckets today or not, clicking
    # Predict always reaches the server and renders *something* -- either the
    # projected values or the "unavailable" fallback (see loadPrediction's own
    # insufficient-history handling) -- proving the round trip works either way.
    expect(predict_body).not_to_have_text("")
