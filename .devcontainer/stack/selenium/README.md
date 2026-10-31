# selenium

A browser (Chromium, via Selenium Grid's standalone image) for the e2e
suite in `tests/e2e/`. `tests/e2e/conftest.py`'s `browser` fixture opens
a WebDriver session against this container purely to read its `se:cdp`
Chrome DevTools Protocol URL, then hands that URL to Playwright's
`connect_over_cdp` — so Playwright, running in the `myapp` container,
drives this remote browser instead of launching a local one. That's what
lets the e2e suite run from inside the devcontainer itself, unlike the
Postgres/RustFS/Keycloak-style pattern of exec-ing into a sibling
container from the host.

- Compose file: `compose.yml`
- Image: `selenium/standalone-chromium`
- Reached from: `tests/e2e/conftest.py`, at `http://selenium:4444`
  (overridable via `E2E_SELENIUM_URL`)
- Target under test: `http://myapp:8000` (via `E2E_BASE_URL`)
- Also reached from: Claude's `playwright` MCP server, the same way —
  see `../../../.claude/mcp/playwright_selenium_bridge.py` and
  `../../../.claude/README.md`.

## Do

- Run the e2e suite from the devcontainer's own terminal, same as the
  other two suites:

  ```bash
  uv run pytest tests/e2e
  ```

- Keep Playwright, pytest-playwright, and selenium (the WebDriver client
  used only to retrieve the CDP URL) in the `dev` optional-dependency
  group in `pyproject.toml`, not pinned again here.

## Don't

- Point `E2E_SELENIUM_URL`/`E2E_BASE_URL` at anything other than this
  stack's own in-network service addresses.
- Install Playwright's own browser binaries (`playwright install`)
  anywhere — this container's Chromium is the only browser the e2e suite
  ever drives.

## Removing this service

Delete this directory, its compose file entry in
`.devcontainer/compose.yml`'s `include:` list, the `browser` fixture
override in `tests/e2e/conftest.py`, the Playwright/selenium packages
from the `dev` optional-dependency group in `pyproject.toml`,
`tests/e2e/`, and the `playwright` MCP server entry plus
`../../../.claude/mcp/playwright_selenium_bridge.py` (see
`../../../.claude/README.md`'s "Removing a tool").
