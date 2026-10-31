# .claude/

Project-scope Claude Code configuration, shared with every collaborator
who opens this repo (see the root `.gitignore`, which does *not* exclude
this directory).

- `settings.json` — enables official plugins from the built-in
  `claude-plugins-official` marketplace via `enabledPlugins`:
  - `pyright-lsp` — gives Claude type diagnostics and code navigation
    from pyright directly, instead of grep-based exploration; this is
    what `scripts/develop.sh` installs `pyright`/`pyright-langserver`
    for.
  - `context7` — pulls up-to-date, version-specific library
    documentation and examples into context from Context7's hosted
    remote MCP server (`https://mcp.context7.com/mcp`); no local
    install. Works anonymously; set a `CONTEXT7_API_KEY` env var for
    higher rate limits.
  - `security-guidance` — security review for Claude-generated code:
    pattern-based warnings on `Edit`/`Write`, an LLM diff review when
    Claude finishes a turn, and an agentic reviewer on `git commit`
    that traces cross-file data flow. Needs only Python 3.8+ on
    `PATH`, which this devcontainer already provides.

  `settings.json` also wires up the `PostToolUse` hook below.
- `hooks/self-check.sh` — after Claude edits a file, runs the fast,
  pre-commit-stage hooks from `../.pre-commit-config.yaml` (ruff, trailing
  whitespace, ...) against just that file via `prek`, so issues are
  caught and auto-fixed immediately instead of first at commit time.
  mypy/pytest/`uv lock --check` stay pre-push-only — too slow to run
  after every edit.
- `agents/plan-executor.md` — the subagent that executes one work item
  of a `docs/plans/` plan on a faster model (`model: sonnet`), under
  the main session as orchestrator (see `../CLAUDE.md`, "Execute
  multi-item work as an orchestrator"). It holds the executor's side of
  the protocol: what a brief contains, the write-set and no-push rules,
  the escalation triggers, and the report format. The same agent, briefed
  to land a finished item, is a lander (its "Lander mode" section):
  diff check, merge, verification lane, PR, `/code-review`, CI and,
  within the plan's standing approvals, merge and release.
- `skills/` — project skills Claude loads when a request matches their
  `description` (or that you invoke as `/<name>`):
  - `plan-creation` — writing a `docs/plans/` plan and, when the user
    has setup to do first, its pre-flight runbook.
  - `plan-execution` — the orchestrator's side of executing a plan:
    the rolling queue of executors and landers, researched answers to
    escalations, serialized lanes, gates and close-out. The main session
    decides and raises gates; landers do the reviewing, merging,
    verifying and releasing.
  - `owasp-scan` — a parallel OWASP Top Ten scan of `src/`.

  Base lists each of these skills one by one in its sync manifest, so an
  instance adds its own as `skills/<name>/` and lists that directory
  (as `.claude/skills/<name>/`, or `.claude/agents/<name>.md` for an
  agent) under `ignore` in `.github/template-sync-manifest.local.yml`
  (base's own entries are `replace` tier, one line per skill and agent; a
  deleted one keeps its line as a tombstone so sync removes it from
  instances, see `../docs/TEMPLATE.md`, "Instance extension points"). That
  entry is what keeps the completeness check passing; sync never reads
  the local file. Don't reuse a base (or removed base) skill or agent
  name, since sync would overwrite or remove it.

  `../CLAUDE.md` points at the two plan skills, so they're used even when
  a request doesn't match their descriptions word for word.
- The `PreToolUse` hook on `Bash` runs `snip hook`, the
  [snip](https://github.com/edouard-claude/snip) token-filtering proxy:
  it rewrites supported commands (this repo's stack — `pytest`, `ruff`,
  `mypy`, `uv` — plus `git` and others) to return only their signal
  (failures, diffs, summaries) instead of full output. `snip` itself is
  installed by `scripts/develop.sh`, pinned to `SNIP_VERSION` in
  `../.devcontainer/Dockerfile`, and checksum-verified against its published release
  checksums — see "Versions and config" in `docs/TEMPLATE.md`. Chosen
  over the more popular
  [rtk](https://github.com/rtk-ai/rtk) because its filters are
  declarative YAML data (auditable, and cover `mypy` — rtk's don't) and
  it fails open (a broken filter passes the command through unfiltered
  rather than blocking it); rtk's larger community and multi-maintainer
  team are the tradeoff, so revisit this choice if `snip` goes
  unmaintained.
- `../.mcp.json` — project-scope MCP servers not available as a
  `claude-plugins-official` plugin, so they can't go through
  `enabledPlugins` above. Each is pinned to an exact version (a commit
  SHA where the project publishes no tagged release) and, where it
  needs the stack's own credentials/endpoints, reads them via `${VAR}`
  from the same env vars `../.devcontainer/compose.instance.yml` already
  gives the `myapp` service — never re-pinned a second time:
  - `clear-thought` (`@waldzellai/clear-thought-onepointfive`) — a
    local stdio MCP server exposing structured reasoning/mental-model
    tools, launched via `npx`. `npx` needs Node.js, which isn't
    otherwise a dependency of this Python template — that's the only
    reason `scripts/develop.sh` installs it (pinned via
    `../.devcontainer/Dockerfile`'s `NODE_VERSION` ARG) — Claude Code itself doesn't need it.
  - `postgres` (`postgres-mcp`, aka Postgres MCP Pro) — query/schema
    access to the `postgres` stack service, run `unrestricted` (full
    read/write against the local dev DB) via `uvx`. Anthropic's own
    reference Postgres MCP server is archived with an unpatched SQL
    injection vulnerability, hence this community one instead. Its
    0.3.0 release itself only pins `mcp[cli]>=1.5.0` with no upper
    bound, so a bare `uvx` resolves the newest (currently incompatible,
    v2) `mcp` and fails at import; the extra `--with mcp==1.9.4` pins
    around that until upstream adds its own bound.
  - `redis` (`redis-mcp-server`) — Redis's own official MCP server for
    the `redis` stack service, run via `uvx`.
  - `s3-mcp` — a generic S3-protocol MCP server (works against any
    S3-compatible endpoint, including RustFS) for the `s3` stack
    service, run via `uvx` from a pinned commit rather than a release
    (none exist). Low trust signal (single maintainer, no stars, no
    tagged release) accepted deliberately; its write-capable tools
    (`put_object`, `delete_object`, `copy_object`, `create_bucket`,
    `delete_bucket`, `presign_put`) are blocked via `settings.json`'s
    `permissions.deny` instead of trusting the server's own config, since
    it has no read-only toggle. RustFS's own MCP server was removed from
    its main branch (per its own docs) and MinIO's was archived in favor
    of a commercial product, so neither was an option.
  - `playwright` (`@playwright/mcp`) — browser automation against the
    `selenium` stack service's Chromium, via `mcp/playwright_selenium_bridge.py`
    (below) rather than a direct `.mcp.json` entry.
- `mcp/playwright_selenium_bridge.py` — `@playwright/mcp` only takes a
  static `--cdp-endpoint`, but `selenium` (Selenium Grid) only hands out
  a CDP URL per WebDriver session — the same one
  `tests/e2e/conftest.py`'s `browser` fixture reads from the `se:cdp`
  capability. This script opens that session itself, launches
  `@playwright/mcp` with the resulting CDP URL, and keeps the session
  open for the MCP server's lifetime (closing it would tear down the
  browser CDP is talking to). Run via `uv run --extra dev` from
  `../.mcp.json`'s `playwright` entry, so it shares the `selenium`
  package already in the `dev` dependency group rather than adding a
  second one.
- `settings.json`'s `permissions`, shared by everyone working here and by
  the `plan-executor` subagents (which inherit the session's rules):
  - `defaultMode: acceptEdits`: file edits in the workspace don't prompt.
  - `allow`: the commands this repo's workflow runs all the time (`uv`,
    `prek`, `git`, `gh`, `docker`, `locust`, `py-spy`, `make`), web
    lookups and context7, so parallel agents don't prompt for routine
    work.
  - `ask`: actions that are user gates in a plan: triggering a
    `release.yml` and setting repo variables (e.g. `PYPI_PUBLISH`).
  - `deny`: `s3-mcp`'s write-capable tools (see above), plus no manual
    PyPI uploads (`uv publish`, `twine`), no force pushes or direct
    pushes to `main`, no `gh pr merge` (the user merges), no
    `gh release create` (releases only through `release.yml`), and no
    `gh repo delete`. Deny always wins over allow.

## Do

- Keep this file's `enabledPlugins` pinned to specific plugin names —
  don't rely on a whole marketplace being auto-installed.

## Don't

- Add personal preferences here — this file is committed and shared;
  use your own user-scope Claude Code settings for those instead.

## Removing a tool

- **pyright-lsp**: delete `settings.json`'s `enabledPlugins` entry and
  remove the `pyright`/`pyright-langserver` install step from
  `scripts/develop.sh`.
- **snip**: delete the `PreToolUse` hook entry in `settings.json` and the
  `snip` install block (and `SNIP_VERSION` ARG in `../.devcontainer/Dockerfile`) from
  `scripts/develop.sh`.
- **context7** / **security-guidance**: delete the plugin's
  `enabledPlugins` entry in `settings.json`.
- **clear-thought**: delete its entry from `../.mcp.json`; if nothing
  else needs Node.js, also remove its install block (and `NODE_VERSION`
  ARG) from `../.devcontainer/Dockerfile`/`scripts/develop.sh`.
- **postgres** / **redis**: delete the entry from `../.mcp.json`.
- **s3-mcp**: delete its entry from `../.mcp.json` and its six
  `permissions.deny` entries from `settings.json`.
- **playwright**: delete its entry from `../.mcp.json` and
  `mcp/playwright_selenium_bridge.py`.
