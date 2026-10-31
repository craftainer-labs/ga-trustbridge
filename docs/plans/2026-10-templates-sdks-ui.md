# Template family, CRUD manifest, client SDKs and UI layer

## Status

Draft (2026-10-19). Starts after
[`2026-10-packages-performance-observability.md`](2026-10-packages-performance-observability.md)
reaches G-final, because this plan touches `lib-crud-py` and this repo's
`Makefile`/`Dockerfile` that the earlier plan is still closing out.
Pre-flight runbook:
[`2026-10-templates-sdks-ui-preflight.md`](2026-10-templates-sdks-ui-preflight.md).

Update 2026-10-30: item AX3 (the axum manifest route) moved to the
template-base plan `2026-10-bff-wellknown-analytics-owasp.md`, item RS2
([link](https://github.com/craftainer/template-base/blob/main/docs/plans/2026-10-bff-wellknown-analytics-owasp.md); that file lives in the template-base workspace until it is
committed there). The rest of this plan stays.

## Goal

Grow the `craftainer` template family from "one FastAPI template and two
Python packages" into a set of layers where each language and artifact type
gets its own template, and where the CRUD layer is usable end to end from
outside the server:

1. **Three new templates**, all instances of template-base:
   `template-docker` (anything that ships an OCI image),
   `template-npm-package` and `template-rust-crate`. `template-fastapi`,
   `template-axum` and `template-react` move onto `template-docker`.
2. **A CRUD manifest**: each CRUD server describes its resources (schemas,
   filterable and sortable fields, capabilities, caching hints) in one
   versioned JSON document, discoverable from a well-known URL, so a client
   can configure itself.
3. **Client SDKs** in Python and TypeScript that read the manifest, with
   caching built in.
4. **`template-react` built for agent-made UI**: a design system, Claude
   skills, the MUI MCP server, Storybook, and a screenshot and accessibility
   feedback loop, so UI that Claude writes looks good and works.
5. **`lib-crud-views-ts`**: an npm package of manifest-driven CRUD views
   (list, detail, create/edit, delete, filter, sort, stats, predictions,
   revisions), each in the five most common variants.
6. **`template-axum` parity**: extract its generic CRUD and observability
   layers into published crates, add the manifest (moved to the [template-base plan](https://github.com/craftainer/template-base/blob/main/docs/plans/2026-10-bff-wellknown-analytics-owasp.md), item RS2), and port the
   measured wins from the FastAPI performance plan, measuring first.

Everything runs as a rolling queue of Sonnet 5.5 `plan-executor` agents
under an Opus 5.5 orchestrator (see "Execution").

## Answers to the questions that started this plan

| Question | Answer | Why |
| --- | --- | --- |
| Create `template-docker` from template-base? | **Yes.** | `template-fastapi`, `template-axum` and `template-react` each carry their own copy of the runtime-image release (multi-arch build, OCI labels per `ADR-0014`, SBOM, manifest-list `finalize`, Dockerfile version check). One layer lets a fix land once and sync everywhere. `template-axum` and `template-react` aren't wired into template sync at all today (no `template-sync-state.json`, not marked as templates), so the move also brings them back into the family. |
| Should crud expose a well-known endpoint with model metadata? | **Yes, through the registered `api-catalog` well-known URI.** | FastAPI's `/openapi.json` already describes shapes, but not CRUD semantics: which fields filter or sort with which operators, whether a resource has drafts, revisions, events, stats or predictions, and its caching hints. Rather than invent an unregistered `/.well-known/crud` (RFC 8615 expects registered names), serve `/.well-known/api-catalog` (RFC 9727, a link set), which points at the OpenAPI document and at a versioned CRUD manifest. OpenID Connect discovery is the precedent for clients configuring themselves from a well-known document. |
| Python and TypeScript SDKs with built-in caching; do we need an npm template? | **Yes to both.** | The npm template is the TypeScript twin of `template-python-package`: the SDK, the views package and anything later share one release contract. Caching layers: HTTP revalidation (`ETag`/`If-None-Match`, already served per `ADR-0023`), in-memory TTL/LRU, a pluggable persistent store (Redis in Python, IndexedDB in the browser), invalidation from the existing SSE `/events` stream, and stale-while-revalidate. |
| Do for `template-axum` what we did for FastAPI? | **Yes, as the last track, measuring first.** | Same split (crates for generic CRUD and observability, published through a new `template-rust-crate`), plus the manifest so the SDKs work against either server. Performance: port the changes the FastAPI A/B program showed to matter (connection pooling, conditional requests, stats cache, OTel traces and metrics, graceful shutdown) after a baseline, not a blind tuning pass. Axum's own `perf.yml` and Goose harness already exist. |
| Best practices for Claude to build good-looking, working UIs, built into `template-react`? | **Yes; see Track UI.** | Anthropic's frontend-design guidance: models default to generic output (Inter, purple gradients, minimal motion) unless a skill gives a specific aesthetic direction (typography, color as variables, motion, backgrounds), loaded only when relevant rather than in the system prompt. Practitioner guidance adds: ground the agent in the real component library (MUI's official MCP server), keep tokens in one place and lint off-system values, and give the agent eyes with a screenshot loop (Playwright, visual regression, accessibility checks). |
| An npm package of common CRUD views in ~5 variants? | **Yes, `lib-crud-views-ts`, on MUI.** | Driven by the manifest, so one component renders any resource. The five variants per view come from a survey of react-admin, Refine, MUI Toolpad and shadcn blocks (item RS2), not from guesswork. List/filter/sort sit on MUI X DataGrid's MIT tier and stats/predictions on MUI X Charts' MIT tier. |

Sources for the answers above:
[Anthropic: improving frontend design through skills](https://claude.com/blog/improving-frontend-design-through-skills),
[MUI MCP server](https://mui.com/material-ui/getting-started/mcp/),
[shadcn/ui MCP](https://ui.shadcn.com/docs/mcp),
[visual regression as an agent feedback loop](https://stevekinney.com/courses/self-testing-ai-agents/visual-regression-as-a-feedback-loop),
[Argos: visual testing for AI coding agents](https://argos-ci.com/blog/visual-testing-ai-coding-agents),
[RFC 8615](https://www.rfc-editor.org/rfc/rfc8615),
[RFC 9727](https://www.rfc-editor.org/rfc/rfc9727),
[crates.io trusted publishing](https://blog.rust-lang.org/2025/07/11/crates-io-development-update-2025-07).

## Target layout

```text
template-base
├── template-python-package ── lib-crud-py, lib-observability-py, lib-crud-sdk-py
├── template-npm-package ───── lib-crud-sdk-ts, lib-crud-views-ts
├── template-rust-crate ────── lib-crud-rs, lib-observability-rs
└── template-docker
    ├── template-fastapi   (uses lib-crud-py, lib-observability-py)
    ├── template-axum      (uses lib-crud-rs, lib-observability-rs)
    └── template-react     (uses lib-crud-sdk-ts, lib-crud-views-ts)
```

### Naming (decided by the user, 2026-10-19)

- **Repos:** libraries are `lib-<name>-<lang>` with `<lang>` one of `py`,
  `ts`, `rs`. Templates keep `template-*`.
- **Registries:** the name without the `lib-` prefix and language suffix,
  prefixed with `craftainer`: `craftainer-<name>` on PyPI and crates.io,
  `@craftainer/<name>` on npm (npm's org-scope convention). Python import
  names match the distribution (`craftainer_crud`).
- **Existing packages are renamed**, which also means new PyPI projects
  (PyPI can't rename): the old projects get a final `0.1.2` (the `0.1.1`
  code, a `DeprecationWarning` on import and a README pointing at the new
  name), then are archived on PyPI, so existing pins keep installing.

| Repo | Registry name (import name) | From |
| --- | --- | --- |
| `lib-crud-py` (renamed from `tool-crud`) | PyPI `craftainer-crud` (`craftainer_crud`), was `craftainer-tool-crud` | template-python-package |
| `lib-observability-py` (renamed from `tool-observability`) | PyPI `craftainer-observability` (`craftainer_observability`), was `craftainer-tool-observability` | template-python-package |
| `template-docker` | none (template) | template-base |
| `template-npm-package` | none (template) | template-base |
| `template-rust-crate` | none (template) | template-base |
| `lib-crud-sdk-py` | PyPI `craftainer-crud-sdk` (`craftainer_crud_sdk`) | template-python-package |
| `lib-crud-sdk-ts` | npm `@craftainer/crud-sdk` | template-npm-package |
| `lib-crud-views-ts` | npm `@craftainer/crud-views` | template-npm-package |
| `lib-crud-rs` | crates.io `craftainer-crud` (`craftainer_crud`) | template-rust-crate |
| `lib-observability-rs` | crates.io `craftainer-observability` (`craftainer_observability`) | template-rust-crate |

Every name above was free on its registry on 2026-10-19. A Python and a
Rust package can share `craftainer-crud`: PyPI and crates.io are separate
namespaces.

## The CRUD manifest (contract I1)

The one interface every track codes against. MF1 writes it as a JSON
Schema (`schemas/crud-manifest/v1.json` in `lib-crud-py`) with fixtures; this
is the starting draft, and MF1 may refine it. Once the orchestrator freezes
it (Decision log), changes go through a new `manifest_version`.

```jsonc
// GET /.well-known/api-catalog  (application/linkset+json, RFC 9727)
{ "linkset": [ { "anchor": "https://host/api",
    "service-desc": [ { "href": "/openapi.json", "type": "application/vnd.oai.openapi+json" } ],
    "describedby":  [ { "href": "/api/_crud/manifest", "type": "application/json",
                        "profile": "https://craftainer.github.io/schemas/crud-manifest/v1" } ] } ] }

// GET /api/_crud/manifest  (ETag + Cache-Control; filtered to what the caller may read)
{ "manifest_version": "1",
  "service": { "name": "…", "version": "1.4.0" },
  "resources": [ {
    "name": "heroes", "title": "Heroes", "api_version": "v1",
    "base_path": "/api/v1/heroes", "formats": ["json", "xml", "web"],
    "id_field": "id",
    "schemas": { "read": { /* JSON Schema */ }, "create": { }, "update": { }, "draft": null },
    "fields": [ { "name": "age", "type": "integer", "filter_ops": ["eq","in","gt","lt"],
                  "sortable": true, "numeric": true, "categorical": false,
                  "read_only": false, "required": true } ],
    "pagination": { "style": "offset", "default_limit": 50, "max_limit": 500 },
    "capabilities": { "bulk_patch": true, "bulk_delete": true, "clone": true,
                      "draft_publish": false, "archive_restore": true, "revisions": true,
                      "events": "sse", "stats": true, "predict": true,
                      "conditional_requests": true, "owner_scoped": false },
    "auth": { "read_roles": ["…"], "write_roles": ["…"], "delete_roles": ["…"] },
    "cache": { "etag": true, "max_age": 0, "stats_ttl": 30 } } ] }
```

Rules: the manifest is built from the same `field_specs`/`describe_fields`
the router already uses (`crud_query.py`), so it can't drift from real
behavior; resources the caller can't read are left out; conformance
fixtures (a request, an expected manifest) live next to the schema, and
the Python server, the Rust server and both SDKs test against the same
fixtures.

## Execution

Executed with the `plan-execution` skill (`.claude/skills/plan-execution/`),
laid out per `README.md`'s "Execution section". The orchestrator's queue,
escalation handling, lanes and gates come from that skill; the executor's
brief, rules, escalation triggers and report format are in
`.claude/agents/plan-executor.md`. Neither is repeated here; this section
holds only this plan's settings.

- **Orchestrator:** Claude Opus 5.5 in the main session. It never
  implements a work item itself; it briefs, answers, merges, verifies and
  runs the serialized lanes.
- **Executors:** `plan-executor`, called with `model: "sonnet"` (Sonnet
  5.5), one per work item, `wip/<ID>` branch, in its own worktree.
- **Concurrency: the skill's rolling queue, target 4 agents at all
  times.** Limits within the 4:
  - at most **1** agent compiling Rust at once, with `CARGO_BUILD_JOBS=4`
    (LTO builds of the axum graph need several GB; raise to 2 only if
    pre-flight P-7 raised Docker's memory to 12 GB or more);
  - **0** other agents while a bench-lane measurement runs (quiet window).
- **Worktrees:**
  - This repo: `isolation: "worktree"`.
  - Other repos: one local clone per repo at
    `.claude/worktrees/ext-<repo>` (the pattern the earlier plan used),
    then `git worktree add .claude/worktrees/ext-<repo>-<ID> -b wip/<ID>`
    per item. The brief names that path as the agent's working directory.
- **Toolchains:** this devcontainer has Python, Node and npm but no pnpm or
  Rust. Agents run each repo's checks inside **that repo's own
  devcontainer image**, built once by the orchestrator (item O0) and run
  with `docker run --rm -v <worktree>:/workspaces/<repo> -w … <image> prek
  run --all-files --hook-stage manual`. Integration tests that need
  Postgres/Redis/S3/MQTT/Keycloak join this devcontainer's existing compose
  network (`CLAUDE.md`: use the devcontainer's own services); nobody starts
  a second database.
- **"Done when" for every brief:** the repo's own
  `prek run --all-files --hook-stage manual` passes in its toolchain
  container (lint, format, type-check, unit tests, coverage gate), **with new
  files staged** (`git add -A` first: the earlier plan hit hooks that skip
  untracked files, twice). Integration and e2e suites run only in the
  verification lane.
- **Library docs:** context7 first, for executors and orchestrator alike.
  Items that pick tools (tsdown, hishel, Storybook, MUI X, crates-io-auth
  action, …) cite the context7 result in their report.

### Answering agents and handling errors

The `plan-execution` skill's protocol applies unchanged: review every
`done` diff, research escalations (context7, then primary sources on the
web, then ADRs and this Decision log), decide only when the research
converges, resume the same agent with `SendMessage`, revert a merge whose
verification lane is red, and re-plan an item that escalates twice on the
same approach. What this plan adds:

- **G-choice** is the gate for decisions the user owns under `CLAUDE.md`'s
  "Decide alone only where a common best practice exists". Known in
  advance: the aesthetic direction for `template-react` (RS3).
- **G-esc** additionally covers a registry or repo name that turns out to
  be taken.

### Serialized lanes (never parallel)

- **Verification lane (orchestrator):** after each merge into a repo's
  integration branch, run that repo's full suite including integration and
  e2e, one at a time across all repos (they share the devcontainer's
  services and fixed ports).
- **Release lane (orchestrator):** template releases (standing approval)
  and package releases (G-rel), in dependency order: the `0.1.2` notice
  releases (RN3, before any `v0.2.0` tag) → templates →
  `craftainer-observability` 0.2.0 → `craftainer-crud` 0.2.0 → SDKs → `lib-crud-views-ts` → axum crates.
- **Bench lane (one long-lived agent, `L2`):** axum baseline and A/Bs, only
  in quiet windows. Acceptance rule unchanged from the earlier plan: median
  of 3 beats the baseline median by more than the larger spread and by at
  least 3%; a change that removes blocking I/O or round trips may be kept
  on correctness grounds with "within noise" recorded.
- **E2E lane (orchestrator):** the full `template-react` + `template-fastapi`
  Playwright run (UI4), and SDK integration runs against a real server.

### User gates

With pre-flight done, only these remain. Same-kind gates in one wave are
batched into one request.

| Gate | Before |
| --- | --- |
| G0 | Kickoff: pre-flight complete, decisions P-D1…P-D9 answered. |
| G-pr | Merging a PR in a repo the user merges (per P-D6). Raised per wave, together. |
| G-rel | The first release of each **published package** (PyPI, npm, crates.io), the `0.1.2` notice releases, and both `0.2.0` releases under the new names. Templates release under the standing approval. An npm first release is staged: the same request asks the user to approve it with 2FA (`npm stage approve <stage-id>` or npmjs.com). |
| G-tp | After a package's first npm or crates.io release: the user adds its trusted publisher on the registry and deletes the bootstrap token (pre-flight P-4/P-5). Batched: one request after Wave 3's releases, one after the axum releases. npm expires a trusted-publisher config that hasn't published within 48 hours, so raise each request just before that package's next release, not earlier. |
| G-choice | Decisions with no common best practice (`CLAUDE.md`, "Decide alone only where a common best practice exists"). Known in advance: the aesthetic direction for `template-react` (RS3). |
| G-legacy | After the `0.1.2` notice releases of `craftainer-tool-crud` and `craftainer-tool-observability`: the user archives both projects on PyPI (project settings, owner only). |
| G-esc | Escalations that are really the user's (the `plan-execution` skill, plus a taken registry or repo name). |
| G-final | Deleting this plan after it's folded into ADRs and docs. |

### Waves (dependencies)

Priority inside the queue follows table order. IDs are new and don't reuse
the earlier plan's.

**Wave 0: foundation and research (after G0).**

| ID | Item | Write set | Depends on |
| --- | --- | --- | --- |
| O0 | *Orchestrator:* local clones of every repo involved; build each repo's devcontainer image as its toolchain image; prove `prek run` works in the `template-react` and `template-axum` images and that they reach this devcontainer's Postgres. | `.claude/worktrees/ext-*` (local only) | G0 |
| RS1 | Docker-layer audit: diff the image-related files of fastapi, axum and react (`Dockerfile`s, `Makefile` image targets, `scripts/builder-*`, `check-dockerfile-versions.sh`, OCI labels, release workflow steps, compose). Output: the exact file list for `template-docker` with sync tiers, and what each instance deletes or renames when it re-parents. | none (report) | G0 |
| RS2 | CRUD views survey: react-admin, Refine, MUI Toolpad, shadcn blocks, Django admin and two SaaS admin UIs. Output: per view, the 5 most common variants with a screenshot reference, the MUI/MUI X (MIT tier only) building blocks, and the props each needs from the manifest. | none (report) | G0 |
| RN1 | Rename `lib-crud-py`'s package: distribution `craftainer-crud`, import `craftainer_crud` (`git mv`, so history follows), every import, docs, README install lines, `release-check`; ADR 0001. The version continues: the first release under the new name is `0.2.0` (with MF2). | `lib-crud-py`: `src/`, `tests/`, `pyproject.toml`, `uv.lock`, docs, `scripts/release-check*` | G0 |
| RN2 | Same for `lib-observability-py` (`craftainer-observability`, `craftainer_observability`), released as `0.2.0` on its own (G-rel); ADR 0001 | `lib-observability-py`: same paths | G0 |
| RN3 | Notice releases: in each of the two repos, a `legacy/0.1.x` branch from `v0.1.1` that keeps the old distribution and import names, adds a `DeprecationWarning` on import and a README pointing at the new name; released as `0.1.2` (G-rel) **before** any `v0.2.0` tag exists, so `release.yml` computes `0.1.2`. Then G-legacy. | the two `legacy/0.1.x` branches only | G0 |
| RS3 | UI-for-agents research, deepening the sources above: skill content (typography, color, motion, layout, density), current Storybook major and whether its MCP addon is stable, Playwright `toHaveScreenshot` determinism in a container (fonts, the stack's Chromium), axe integration, a lint guard for off-token colors in `sx`/`styled`. Output: draft `SKILL.md` texts, a tool list with versions, and **three aesthetic directions** (font pairing, palette, density, motion), each rendered as a screenshot of the current Heroes page, for the user to pick (G-choice; no best practice decides taste). | none (report) | G0 |
| RS4 | Axum gap analysis: features `template-fastapi`/`lib-crud-py` gained since axum's parity merge (conditional requests, stats cache, pool modes, OTel traces/metrics, request IDs, graceful shutdown, `/health/info`, …), the extraction boundary for `src/generic/` and `src/health/` + telemetry, and the state of `perf.yml`/Goose. Output: a numbered item list for Track AX with write sets. | none (report) | G0 |
| RS5 | SDK design: caching libraries (hishel for httpx vs own layer; browser storage), TanStack Query integration shape, SSE-driven invalidation against the real `/events` stream, auth hooks (bearer token provider), problem-details errors (`ADR-0004`). Output: module layout and the I2/I3 interfaces. | none (report) | G0 |

The orchestrator turns RS1–RS5 into Decision log rows and into the briefs
below. Findings the sources don't settle go to the user (G-choice);
findings that change scope go to the user too (G-esc). RN1 and RN2 rewrite
nearly every file in their repo, so nothing else runs in those repos until
they're merged.

**Wave 1: contract and templates (after Wave 0's decisions).**
The orchestrator first creates `template-docker`, `template-npm-package`
and `template-rust-crate` from template-base (standing approval), marks
them as templates and prepares their rulesets (P-D6).

| ID | Item | Write set | Depends on |
| --- | --- | --- | --- |
| MF1 | Manifest contract I1: JSON Schema, spec doc, conformance fixtures, `lib-crud-py` ADR 0002 | `lib-crud-py`: `schemas/`, `docs/manifest.md`, `docs/adrs/0002-*`, `tests/fixtures/manifest/` | RS5, RN1 |
| B2 | template-base: `release.yml` passes `NPM_PUBLISH`/`CARGO_PUBLISH` variables, `id-token: write` already there, and the bootstrap secrets (`NPM_TOKEN`, `CARGO_REGISTRY_TOKEN`) only to `publish`; docs | template-base: `.github/workflows/release.yml`, its README | G0 |
| DK1 | `template-docker`: `Makefile` image contract (multi-arch build, SBOM, GHCR publish, `finalize` manifest list, `release-check` image smoke), `scripts/` it needs | template-docker: `Makefile`, `scripts/` | RS1 |
| DK2 | `template-docker`: hooks (hadolint, Dockerfile version check, image scan), sync manifest, `docs/TEMPLATE.md` section, ADR 0001, Renovate docker datasource | template-docker: `.pre-commit-config.yaml`, `.github/`, `docs/` | RS1 |
| NP1 | `template-npm-package` scaffold: pnpm via Corepack in `scripts/develop.d/`, `package.json`, strict `tsconfig`, ESM build (tool per RS5/context7), `src/`, `tests/`, vitest with a 100% gate, oxlint, prettier, publint, arethetypeswrong; all dependencies and the lockfile | template-npm-package: those paths, `.pre-commit-config.yaml` | B2 |
| NP2 | `template-npm-package` release: `Makefile` (`npm pack`, SBOM, `npm publish --provenance` via OIDC; without a trusted publisher, `npm stage publish` with the stage-only bootstrap token; npm ≥ 11.15.0), `release-check` (install from the registry and import), docs, ADR 0001 | template-npm-package: `Makefile`, `scripts/release-*`, `docs/`, `.github/` | B2 |
| RC1 | `template-rust-crate` scaffold: rustup in `scripts/develop.d/`, `Cargo.toml` workspace, clippy `-D warnings`, rustfmt, cargo-llvm-cov gate, cargo-deny, cargo-semver-checks; `Cargo.lock` | template-rust-crate: those paths, `.pre-commit-config.yaml` | B2 |
| RC2 | `template-rust-crate` release: `Makefile` (`cargo package`, SBOM, `cargo publish` via crates.io trusted publishing with the bootstrap-token fallback), `release-check`, docs, ADR 0001 | template-rust-crate: `Makefile`, `scripts/release-*`, `docs/`, `.github/` | B2 |

Then (release lane): template-base minor release, sync PRs into the three
new templates, and `v1.0.0` of each new template.

**Wave 2: implementations (after Wave 1's templates are released and
MF1 is frozen).** The orchestrator creates `lib-crud-sdk-py`,
`lib-crud-sdk-ts` and `lib-crud-views-ts` from their templates.

| ID | Item | Write set | Depends on |
| --- | --- | --- | --- |
| MF2 | `lib-crud-py`: manifest builder from `field_specs`, `/_crud/manifest` route with `ETag`, caller-filtered output, `api-catalog` router helper; conformance tests against MF1's fixtures; release `0.2.0` (G-rel) | `lib-crud-py`: `src/craftainer_crud/manifest/`, a new `controllers/crud_manifest.py`, their tests, package README section | MF1 |
| PY0 | `lib-crud-sdk-py`: every dependency and extra (`httpx`, cache and Redis and SSE extras per RS5) and the lockfile; package skeleton and I2 stubs | `pyproject.toml`, `uv.lock`, `src/…/__init__.py`, `src/…/interfaces.py` | template-python-package, RS5 |
| PY1 | Python client core: manifest loading, sync and async resource clients, query builder matching `crud_query` semantics, pagination iterators, problem-details exceptions, auth hook | `src/…/{client,query,errors,auth}.py`, their tests | PY0, MF1 |
| PY2 | Python caching: revalidation, TTL/LRU memory store, Redis store, SSE invalidation, stale-while-revalidate; per-resource policy defaulting from the manifest `cache` hints | `src/…/cache/`, `src/…/events.py`, their tests | PY0, MF1 |
| TS0 | `lib-crud-sdk-ts`: dependencies, lockfile, skeleton, I3 stubs, subpath `exports` map (`.`, `./cache`, `./tanstack`) | `package.json`, `pnpm-lock.yaml`, `src/index.ts`, `src/interfaces.ts` | template-npm-package, RS5 |
| TS1 | TS client core: zero-dependency `fetch` client, manifest-typed resources, query builder, errors, auth; `codegen` CLI that writes TS types from a manifest | `src/{client,query,errors,auth}.ts`, `src/codegen/`, their tests | TS0, MF1 |
| TS2 | TS caching: revalidation, memory and IndexedDB stores, SSE invalidation, stale-while-revalidate; `./tanstack` (`queryOptions` factories, key factory, invalidation bridge) | `src/cache/`, `src/events.ts`, `src/tanstack/`, their tests | TS0, MF1 |
| UI0 | `template-react` re-parent: bootstrap template sync onto `template-docker`, one-time Dockerfile layout upgrade (`app.Dockerfile` → `Dockerfile`), mark as template; plus every dependency Track UI needs (Storybook, Playwright, axe, Motion, fonts, TanStack Query) and the lockfile | template-react: sync files, `Dockerfile*`, `Makefile`, `package.json`, `pnpm-lock.yaml`, ADR 0003 | DK1, DK2, RS3 |
| UI1 | Design system in the direction the user picked from RS3: MUI theme with CSS variables and light/dark, typography with the chosen self-hosted font pairing, spacing and radius scale, motion tokens; lint hook rejecting hex/rgb colors and raw px outside `src/theme/`; `docs/design/` principles | template-react: `src/theme/`, `scripts/check-design-tokens.sh`, its hook, `docs/design/`, ADR 0004 | UI0, RS3's G-choice |
| UI2 | Claude assets: `.claude/skills/frontend-design/SKILL.md` (aesthetic direction, anti-defaults, "vary across generations"), `.claude/skills/ui-verify/SKILL.md` (start dev server, screenshot 3 viewports × light/dark with the Playwright MCP, run axe, compare against the stated intent, iterate), `@mui/mcp` in `.mcp.json`, README and `CLAUDE.md`-adjacent docs | template-react: `.claude/skills/`, `.mcp.json`, `.claude/README.md` | UI0 |
| UI3 | Storybook for every component, Playwright visual regression (`toHaveScreenshot`) and axe checks over the stories, baselines generated in the toolchain container against the stack's Chromium, CI job | template-react: `.storybook/`, `src/**/*.stories.tsx`, `tests/visual/`, `.github/workflows/visual.yml`, ADR 0005 | UI0 |
| AX0 | `template-axum` re-parent onto `template-docker` (same shape as UI0), mark as template | template-axum: sync files, `Dockerfile*`, `Makefile`, ADR 0019 | DK1, DK2 |
| FA0 | `template-fastapi` re-parent onto `template-docker`: sync state, drop files template-docker now owns, manifest tiers | this repo: `.github/template-sync-*`, `Makefile`, `scripts/` (per RS1), ADR 0024 | DK1, DK2, earlier plan's G-final |

Then (release lane): `craftainer-crud` 0.2.0 (G-rel), client 0.1.0 releases
(G-rel; TS staged via the bootstrap token and approved by the user, then G-tp), template-docker sync PRs.

**Wave 3: views and server wiring (after `craftainer-crud` 0.2.0 and
`@craftainer/crud-sdk` 0.1.0 are published).**

| ID | Item | Write set | Depends on |
| --- | --- | --- | --- |
| VW0 | `lib-crud-views-ts` core: dependencies (MUI, MUI X MIT tier, TS SDK, TanStack Query as peers), `<CrudProvider>`, `useResource`, field-renderer registry (contract I4), layout shells, Storybook + visual setup copied from UI3 | `package.json`, lockfile, `src/core/`, `.storybook/`, `tests/visual/` setup | TS1, TS2 released, UI3, RS2 |
| VW1 | List, filter and sort: 5 variants per RS2 (e.g. data grid, card grid, compact list, grouped board, timeline), filter bar from `filter_ops`, URL-synced state | `src/list/`, `src/filter/`, stories, tests | VW0 |
| VW2 | Create, edit, delete: 5 form variants per RS2 (e.g. page, dialog, drawer, stepper, inline) generated from the `create`/`update` schemas, `If-Match` on save, delete confirm and undo | `src/form/`, `src/delete/`, stories, tests | VW0 |
| VW3 | Detail, revisions, bulk actions, draft/publish, archive/restore, shown only when `capabilities` says so | `src/detail/`, `src/revisions/`, `src/bulk/`, stories, tests | VW0 |
| VW4 | Stats and predictions: 5 dashboard variants per RS2 on MUI X Charts, from `numeric`/`categorical` fields and `/predict` | `src/stats/`, stories, tests | VW0 |
| FA1 | `template-fastapi`: switch to `craftainer-crud` and `craftainer-observability` 0.2.0 (new imports everywhere), mount the manifest and `api-catalog`, e2e test reading the manifest through the Python SDK | this repo: `pyproject.toml`, `uv.lock`, `src/`, `tests/`, living `README.md`s, ADR 0025 | MF2 and RN2 released, PY1 |
| AX1 | `lib-crud-rs`: `git filter-repo` history of `src/generic/` into the new repo, crate layout, tests against the devcontainer Postgres | `lib-crud-rs` repo | RC1, RC2 released, RS4 |
| AX2 | `lib-observability-rs`: same for `src/health/`, telemetry, request IDs | `lib-observability-rs` repo | RC1, RC2 released, RS4 |
| L2 | Axum baseline on an image from `template-axum`'s `main` (quiet window) | none (report) | AX0 |

Then: `lib-crud-views-ts` 0.1.0 (G-rel, then G-tp), Python client against
`template-fastapi` in the E2E lane.

**Wave 4: integration and axum parity.**

| ID | Item | Write set | Depends on |
| --- | --- | --- | --- |
| UI4 | `template-react` uses `lib-crud-sdk-ts` + `lib-crud-views-ts`: a manifest-driven admin replaces `HeroesPage`; Playwright e2e against `template-fastapi` (E2E lane) | template-react: `src/pages/`, `src/App.tsx`, `src/api/`, `tests/e2e/`, ADR 0006 | VW1–VW4 released, FA1 |
| AX4… | Parity items from RS4 (conditional requests, stats cache, pool settings, OTel traces and metrics, graceful shutdown), one item each with disjoint write sets, each A/B-measured by L2 | per RS4 | AX1/AX2, L2 baseline |
| AX9 | `template-axum` switches to the published crates and deletes the moved code | template-axum: `Cargo.toml`, `Cargo.lock`, `src/`, `tests/`, ADR 0020 | AX1, AX2, AX4… released; template-base plan RS2 landed (axum manifest) |

**Wave 5: close-out.**

| ID | Item | Write set |
| --- | --- | --- |
| DOC1 | Fold this plan's outcomes into ADRs/docs per repo (manifest in `lib-crud-py`, family layout in template-base `docs/TEMPLATE.md`, design workflow in template-react) | docs only, per repo |
| SY1 | Template-sync PRs so every instance is on its template's latest release | sync PRs only |

Then the final verification lane over every repo, G-final, and this file
is deleted per `docs/plans/README.md`.

### Interfaces between parallel items

- **I1** CRUD manifest v1 (above), frozen after MF1.
- **I2** Python: `class CacheStore(Protocol)` (`get`, `set(ttl)`, `delete`,
  `clear`) and a client transport hook (`on_request`/`on_response`) that
  PY2 plugs into; fixed in PY0.
- **I3** TypeScript: the same as `interface CacheStore` and a middleware
  chain on the `fetch` client; fixed in TS0.
- **I4** Views: `CrudProvider({ client })`, `useResource(name)` returning
  the manifest entry plus SDK handle, and `registerFieldRenderer(type,
  component)`; fixed in VW0.

### Pre-assigned numbers

| Repo | Numbers |
| --- | --- |
| template-fastapi (here) | ADR 0024 (FA0 re-parent), ADR 0025 (FA1 manifest) |
| lib-crud-py | ADR 0001 (RN1 rename), 0002 (MF1 manifest) |
| lib-observability-py | ADR 0001 (RN2 rename) |
| template-docker / template-npm-package / template-rust-crate | ADR 0001 each (DK2, NP2, RC2) |
| template-react | ADR 0003 (UI0), 0004 (UI1), 0005 (UI3), 0006 (UI4) |
| template-axum | ADR 0019 (AX0), 0020 (AX9); RS4 assigns 0021+ to AX4… |
| lib-crud-sdk-py / -ts, lib-crud-views-ts | ADR 0001 each (PY2, TS2, VW0) |

### Standing approvals (granted at G0, pre-flight P-D6)

The orchestrator may, without asking: create the repos in "Target layout";
mark templates; push branches and open PRs in every repo this plan touches;
trigger `release.yml` in template-base and the three new templates; copy
rulesets onto repos it created; and, if P-D6 says so, merge PRs in repos it
created once CI is green and a `/code-review` pass found nothing blocking.

## Approach notes per track

**Templates (DK, NP, RC).** Model each on `template-python-package`'s
release contract (template-base's `Makefile` targets; version only in the
tag; `make build` stamps it; `release-check` installs the published
artifact back). npm and crates.io have no pending publishers: a package's
first release uses a short-lived, scope-limited bootstrap token from
pre-flight, then trusted publishing takes over (G-tp). On npm that token
is stage-only: the first release is `npm stage publish`, which creates a
public `0.0.0-stage` placeholder and holds the real version until the
user approves it with 2FA (npm ≥ 11.15.0; npm is removing direct
publishing for 2FA-bypass tokens, targeted January 2027). npm's OIDC path
needs npm ≥ 11.5.1 and breaks if `NODE_AUTH_TOKEN` is set to an empty
string, so `publish` unsets it rather than blanking it.

**Manifest (MF).** Built from code the router already runs, so the
manifest can't disagree with what the API accepts. `ETag` and
`Cache-Control` reuse `ADR-0023`'s helpers. Role filtering means a
client never learns that a hidden resource exists.

**SDKs (PY, TS).** One behavior spec, two languages; the MF1 fixtures plus
a shared list of cache scenarios (fresh hit, `304` revalidation, SSE
invalidation, write-through after `PATCH`, `412` on a stale `If-Match`)
are the tests both must pass. Caching is opt-in per resource with
manifest-derived defaults; nothing caches a response that carried
`Cache-Control: no-store`.

**UI (UI, VW).** MUI stays (P-D4) because it's already in the template,
has an official MCP server that gives Claude real component APIs, and MUI
X's MIT tier covers grids and charts. The design skill gives direction,
not hex values; tokens live in the theme; a lint hook blocks off-token
values; Storybook makes every component visible in isolation; visual
regression and axe make "looks right and is usable" a pass/fail check,
and `ui-verify` gives Claude the screenshot loop while it works.

**Axum (AX, L2).** Measure first (L2 baseline), then port only what the
gap analysis lists, each A/B-tested with the earlier plan's rule.

## Decision log

| Date | Decision |
| --- | --- |
| 2026-10-19 | Plan drafted. Discovery goes through `/.well-known/api-catalog` (RFC 9727) pointing at `/_crud/manifest`, not an unregistered `/.well-known/crud`. Confirmed or changed in pre-flight P-D2. |
| 2026-10-19 | Concurrency: rolling queue, 4 agents; at most 1 compiling Rust; 0 during bench measurements. |
| 2026-10-19 | First npm and crates.io publishes use pre-flight bootstrap tokens; trusted publishing afterwards (G-tp). Neither registry supports pending publishers. |
| 2026-10-20 | User decision: the npm bootstrap token is **stage-only** ("Read and write (stage only)"), per npm's deprecation of direct publishing with 2FA-bypass tokens ([discussion](https://github.com/orgs/community/discussions/201329), [staged publishing](https://docs.npmjs.com/staged-publishing)). First npm releases are staged and approved by the user at G-rel; NP2 needs npm ≥ 11.15.0. |
| 2026-10-19 | User decision: the orchestrator decides alone only where a common best practice exists; anything else goes to the user with options, sources and a labeled lean (G-choice). A standing rule in `CLAUDE.md` since template-base v1.6.0. |
| 2026-10-19 | User decision, naming: repos `lib-<name>-<py\|ts\|rs>`; registries `craftainer-<name>` (PyPI, crates.io) and `@craftainer/<name>` (npm); SDKs are `crud-sdk`; Python import names match the distribution (`craftainer_crud`, `craftainer_observability`, `craftainer_crud_sdk`). `tool-crud`/`tool-observability` are renamed on GitHub in pre-flight. |
| 2026-10-19 | User decision, old PyPI projects: `craftainer-tool-crud` and `craftainer-tool-observability` get a final `0.1.2` notice release (old code, `DeprecationWarning`, README pointer), then are archived on PyPI, not yanked, so existing pins keep working (RN3, G-legacy). |
| 2026-10-30 | Z1 trim: the axum api-catalog and manifest (AX3) moved to the template-base plan [`2026-10-bff-wellknown-analytics-owasp.md`](https://github.com/craftainer/template-base/blob/main/docs/plans/2026-10-bff-wellknown-analytics-owasp.md), item RS2 (user decision of 2026-10-29: "AX3/AX4 (axum catalog/manifest)"). That file lives in the template-base workspace until committed. AX4… (RS4's parity items: conditional requests, stats cache, pool settings, OTel, graceful shutdown) stays here, since RS2 there covers only the catalog and manifest; to be confirmed. AX9 now also waits for RS2 there. "RS2" in VW0 and the Track RS table is this plan's views survey, not that item. |

## Open questions

All open questions are pre-flight decisions P-D1…P-D9 in the runbook; the
orchestrator copies the answers here at G0 and removes this section.
