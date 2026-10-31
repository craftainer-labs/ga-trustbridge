# Pre-flight runbook: template family, CRUD manifest, SDKs and UI plan

Setup for the user to finish **before** kickoff (gate G0) of
[`2026-10-templates-sdks-ui.md`](2026-10-templates-sdks-ui.md). Part 1 is
decisions (answer them once, so the run doesn't stop to ask). Part 2 is
setup only you can do (accounts, org settings, local machine). Each step
says how to check it worked. Steps P-1b and P-3 to P-6 need an owner of the
`craftainer` GitHub organization.

This file belongs to the plan and is deleted with it.

## Checklist

- [x] P-1. Close the earlier plan
- [x] P-1b. Rename the two package repos
- [x] P-2. Answer decisions P-D1…P-D9 (Part 1)
- [x] P-3. PyPI and TestPyPI: publishers for the new and renamed projects
- [x] P-4. npm: org and bootstrap token (`NPM_TOKEN` expires 2026-11-19)
- [x] P-5. crates.io: account and bootstrap token (`CARGO_REGISTRY_TOKEN` expires ~2026-12-19)
- [x] P-6. GitHub org: check secrets, apps and packages
- [x] P-7. Docker resources (host has 8 GB total; Docker stays at ~7.7 GB, P-D8 unchanged)
- [x] P-8. Local permissions
- [x] P-9. Power settings
- [ ] P-10. Start a fresh session and kick off

---

## Part 1: decisions

Tick one option per decision (or write your own). The first option is the
recommendation. The orchestrator copies the answers into the plan's
Decision log at G0 and never reopens them.

**P-D1. `template-docker` layer.**
- [x] Create it; `template-fastapi`, `template-axum` and `template-react`
  sync from it (recommended).
- [ ] Skip it; each app template keeps its own image release.

**P-D2. Where clients discover the CRUD manifest.**
- [x] `/.well-known/api-catalog` (RFC 9727, a registered well-known name)
  linking to `/openapi.json` and `/api/_crud/manifest` (recommended).
- [ ] An unregistered `/.well-known/crud-manifest` serving the manifest
  directly (simpler, non-standard).

Also: should the manifest require authentication?
- [x] Same roles as reading the resource; unauthenticated callers see only
  public resources (recommended).
- [ ] Always public.

**P-D3. Names: decided 2026-10-19** (the plan's "Naming" section and
Decision log): repos `lib-<name>-<py|ts|rs>`; PyPI and crates.io
`craftainer-<name>`; npm `@craftainer/<name>`; SDKs are `crud-sdk`;
Python import names match (`craftainer_crud`); `tool-crud` and
`tool-observability` are renamed (P-1b), and their old PyPI projects get a
`0.1.2` notice release and are then archived. Nothing to tick.

**P-D4. UI library for `template-react` and `lib-crud-views-ts`.**
- [x] Stay on MUI (recommended): already in the template, official MCP
  server for Claude, MUI X DataGrid and Charts (MIT tier) cover
  list/filter/sort/stats, and it ships as a normal npm dependency.
- [ ] Switch to shadcn/ui + Tailwind: strong agent tooling (registry +
  MCP), but components are copied into each app rather than versioned in a
  package, so `lib-crud-views-ts` would become a shadcn registry instead of
  an npm package, and `template-react` is rewritten.

**P-D5. Axum track in this plan.**
- [x] Yes, as the last waves (recommended).
- [ ] No, a separate plan later (drops RC1/RC2, AX*, L2; `template-rust-crate`
  is then created later too).

**P-D6. Who merges PRs.**
- [x] The orchestrator merges PRs **in repos this plan creates**, once CI is
  green and a `/code-review` pass found nothing blocking; you merge PRs in
  existing repos (template-base, template-fastapi, lib-crud-py,
  lib-observability-py, template-react, template-axum) (recommended: cuts
  most waits).
- [ ] You merge every PR (more control; the run waits for you each wave).

If the first: does each new repo's "Protect main" ruleset require an
approving review? An orchestrator can't approve its own PR. Either the
ruleset requires a PR but no approvals (as on the earlier repos, if that's
how you set them up), or pick the second option.

**Answer (2026-10-20):** the earlier repos' "Protect main" requires 1
approval, so each new repo's ruleset copy sets
`required_approving_review_count` to **0** (PR still required).

**P-D7. License for the new libraries (SDKs, views, crates).**
- [x] GPL-3.0, like every other repo (recommended for consistency).
- [ ] A permissive license (MIT or Apache-2.0) for libraries apps link
  against, so apps under other licenses can use them; templates stay GPL.

**P-D8. Concurrency.** 4 agents, at most 1 compiling Rust.
- [x] Yes (recommended at the current ~7.7 GB of Docker memory).
- [ ] Other, after raising memory in P-7: …… agents, …… Rust.

**P-D9. CRUD view variants.** RS2 picks the five variants per view from its
survey.
- [x] The orchestrator decides from RS2's counts and records it
  (recommended; no stop). Ties or close calls still come to you
  (G-choice).
- [ ] Show me RS2's report before Wave 3 starts (one extra gate).

## Part 2: setup

## P-1. Close the earlier plan

`2026-10-packages-performance-observability.md` must reach G-final first:
the 0.1.1 releases, A6, the integration PR into `main`, and the plan file
deleted. This plan's FA0/FA1 change files the earlier plan is still using.

**Check:** `git switch main && git pull` shows
`docs/plans/2026-10-templates-sdks-ui.md` and no longer shows
`2026-10-packages-performance-observability.md`.

## P-1b. Rename the two package repos

After P-1 (the earlier plan has open PRs in both repos). For each row:
<https://github.com/craftainer/REPO/settings> → **General → Repository
name** → **Rename**.

| Old | New |
| --- | --- |
| `tool-crud` | `lib-crud-py` |
| `tool-observability` | `lib-observability-py` |

GitHub redirects the old URLs, git remotes and API calls, so nothing
breaks right away; the orchestrator updates its local clones (O0) and the
links in docs (RN1, RN2, FA1). Don't create new repos with the old names:
that ends the redirect.

**Check:** `gh repo view craftainer/lib-crud-py --json name` and
`gh repo view craftainer/lib-observability-py --json name` both print the
new name.

## P-2. Answer the decisions

Tick the boxes in Part 1 and commit, or paste your answers into the
kickoff message (P-10).

## P-3. PyPI and TestPyPI: publishers for the new and renamed projects

Do this on **both** PyPI and TestPyPI. The earlier plan has the account
links.

**3a. Pending publishers for the three new projects**
(<https://pypi.org/manage/account/publishing/> and
<https://test.pypi.org/manage/account/publishing/> → **Add a new pending
publisher → GitHub**; owner `craftainer`, workflow `release.yml`,
environment empty):

| PyPI Project Name | Repository name |
| --- | --- |
| `craftainer-crud` | `lib-crud-py` |
| `craftainer-observability` | `lib-observability-py` |
| `craftainer-crud-sdk` | `lib-crud-sdk-py` |

**3b. Point the two old projects at the renamed repos**, so their `0.1.2`
notice releases (RN3) can publish. PyPI matches a publisher on the
repository name, which changed in P-1b. For `craftainer-tool-crud` and
`craftainer-tool-observability`: <https://pypi.org/manage/project/craftainer-tool-crud/settings/publishing/>
(and the same for the other project, and on TestPyPI) → add a GitHub
publisher with repository `lib-crud-py` (or `lib-observability-py`),
workflow `release.yml`, then remove the old `tool-*` entry.

**Check:** the account publishing page lists three pending publishers;
each old project's publishing page lists only the `lib-*` repository.

## P-4. npm: org and bootstrap token

npm has no pending publishers: a package's **first** version needs a
token; trusted publishing is configured after it exists (gate G-tp, mid-run).

1. Sign in at <https://www.npmjs.com> with 2FA enabled.
2. Create the org: <https://www.npmjs.com/org/create>, name `craftainer`,
   **Unlimited public packages (free)**.
3. Create a **granular access token** (avatar → Access Tokens → Generate New
   Token → Granular Access Token):
   - Packages and scopes: **Read and write (stage only)**, limited to
     the `@craftainer` scope. npm is removing direct publishing for
     2FA-bypass tokens (targeted January 2027), so CI only *stages* the
     first release (`npm stage publish`) and you approve it with 2FA at
     G-rel (decided 2026-10-20, see the plan's Decision log);
   - Organizations: no access;
   - **Bypass two-factor authentication** for automation: on (CI can't
     answer a 2FA prompt; with stage-only it can't publish on its own);
   - Expiration: long enough to cover the run (30 days or more; npm may cap
     it lower, in which case note the date and tell the orchestrator).
4. Add it as a GitHub **organization secret** `NPM_TOKEN`:
   <https://github.com/organizations/craftainer/settings/secrets/actions>,
   repository access **All repositories** (the repos don't exist yet).
   `release.yml` passes it only to the `publish` step (item B2).

**Check:** the secrets page lists `NPM_TOKEN`; in the devcontainer,
`npm view @craftainer/crud-sdk` and
`npm view @craftainer/crud-views` print **404** (names free).

## P-5. crates.io: account and bootstrap token

Same situation as npm. Skip this step if P-D5 is "No".

1. <https://crates.io> → **Log in with GitHub**, then verify your email
   under **Account Settings** (crates.io refuses publishes without it).
2. **Account Settings → API Tokens → New Token**:
   - Scopes: **publish-new** and **publish-update**;
   - Crates: pattern `craftainer-*`;
   - Expiration: 60 days.
3. Add it as organization secret `CARGO_REGISTRY_TOKEN`, **All
   repositories**.

**Check:** the secrets page lists it, and
<https://crates.io/crates/craftainer-crud> and
<https://crates.io/crates/craftainer-observability> show "not
found".

## P-6. GitHub org: check what the earlier plan set up

1. **`TEMPLATE_SYNC_TOKEN`** (org secret, All repositories): still present,
   and its token's expiry (at
   <https://github.com/settings/personal-access-tokens>) is after the
   expected end of the run (allow 3 weeks). Renew it if not.
2. **Renovate** and **CodSpeed** apps still on **All repositories**
   (<https://github.com/organizations/craftainer/settings/installations>).
3. **Packages:** <https://github.com/organizations/craftainer/settings/packages>
   allows public packages, so instances of `template-docker` can push
   images to GHCR.

**Check:** all three as described.

## P-7. Docker resources

Rust release builds with LTO and the full axum dependency graph need
several GB each, on top of the running stack.

1. Docker Desktop → Settings → Resources: if you can, raise memory to **12
   GB or more** and tell the orchestrator (P-D8); otherwise leave it.
2. Make sure at least **40 GB** of disk is free for images, Rust `target/`
   directories and Playwright baselines: `docker system df` in the
   devcontainer, and prune old images if needed.

**Check:** `free -g` in the devcontainer shows the memory you set.

## P-8. Local permissions

The shared `.claude/settings.json` already allows `git`, `gh`, `docker`,
`uv`, `prek`, `make`, WebSearch, WebFetch and context7, and asks before
`gh workflow run release.yml` and `gh variable set` (so each release still
shows one prompt: that's intended). Add to your **local**
`.claude/settings.local.json` (not committed):

```json
{
  "permissions": {
    "allow": [
      "Bash(npm view *)",
      "Bash(npx @devcontainers/cli *)",
      "Bash(gh api repos/craftainer/*/rulesets*)",
      "Bash(gh api --method POST repos/craftainer/*/rulesets*)"
    ]
  }
}
```

The ruleset rules exist because in the earlier plan auto mode's classifier
refused the orchestrator's ruleset copy, and you had to apply it by hand
for every new repo; this plan creates up to eight.

If P-D6 is the first option, also allow merging in the new repos only,
one rule per repo (the orchestrator always puts `--repo` first):

```json
"Bash(gh pr merge --repo craftainer/template-docker *)",
"Bash(gh pr merge --repo craftainer/template-npm-package *)",
"Bash(gh pr merge --repo craftainer/template-rust-crate *)",
"Bash(gh pr merge --repo craftainer/lib-crud-sdk-py *)",
"Bash(gh pr merge --repo craftainer/lib-crud-sdk-ts *)",
"Bash(gh pr merge --repo craftainer/lib-crud-views-ts *)",
"Bash(gh pr merge --repo craftainer/lib-crud-rs *)",
"Bash(gh pr merge --repo craftainer/lib-observability-rs *)"
```

**Check:** `/permissions` in Claude Code lists the new rules.

## P-9. Power settings

Turn off sleep for the run (Windows: *Power & sleep → Never*; macOS:
*Battery/Energy → Prevent automatic sleeping*).

## P-10. Start a fresh session and kick off

1. Open a **new** Claude Code conversation on **Opus** (`/model`).
   Don't continue the planning conversation.
2. Check: `/agents` lists **plan-executor** (Sonnet), `/mcp` shows
   context7 and playwright connected, `gh auth status` shows `Logged in`.
3. Send:

   > Execute `docs/plans/2026-10-templates-sdks-ui.md` as orchestrator, per
   > `CLAUDE.md`. Pre-flight P-1…P-10 (with P-1b) is complete and the decisions are
   > ticked in the runbook. G0 is granted. Keep 4 agents running.

From then on, the orchestrator comes back to you only for: PR merges in
existing repos (G-pr, batched per wave), package releases (G-rel), adding
trusted publishers after first npm/crates.io releases and deleting the
bootstrap tokens (G-tp, two batched requests), archiving the two old PyPI
projects (G-legacy), decisions with no common best practice, such as the UI's
aesthetic direction (G-choice), release-workflow permission prompts, real
escalations (G-esc), and the close-out (G-final).
