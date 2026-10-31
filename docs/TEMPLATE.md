# Template conventions

Everything about this repository's structure, tooling, and workflow that's
identical across every instance of this template — as opposed to the root
`README.md`'s short, instance-owned preface. See `../CLAUDE.md`'s "Keeping
this file current" for where a new convention belongs.

## Contents

- `.devcontainer/` — the devcontainer setup; see its `README.md`.
- `.github/` — CI and release workflows; see its `CONTENTS.md`.
- `.vscode/` — editor settings, tasks, launch config; see its `README.md`.
- `.claude/` — Claude Code CLI project config; see its `README.md`.
- `.mcp.json` — project-scope MCP servers not covered by a
  `.claude/settings.json` plugin; see `.claude/README.md`.
- `src/` — the application source; see its `README.md`.
- `alembic/` / `alembic.ini` — database migrations; see
  `alembic/README.md`.
- `tests/` — automated tests, split into `unit/`, `integration/`,
  `e2e/` (Playwright), and `perf/` (Locust) suites; see its `README.md`.
- `scripts/` — both Dockerfiles' per-stage setup scripts; see its
  `README.md`.
- `docs/` — knowledge about what the app does; this file is the one
  exception, documenting the template itself rather than product/domain
  knowledge.
- `.secrets/` — local secret files, never committed; see its `README.md`.
- `.devcontainer/Dockerfile` — a single `develop` stage (the devcontainer
  image), with its build-context excludes in
  `.devcontainer/Dockerfile.dockerignore`; owned by template-base.
- `Dockerfile` (+ `.dockerignore`) — the app image's `builder` and
  `runner` stages, and the repo's only top-level Dockerfile; see
  "Instance extension points" below.
- `scripts/develop.sh`, `scripts/post-create.sh` — the `develop` stage's
  setup script and the devcontainer's `postCreateCommand`, each running
  an instance hook directory (`develop.d/`, `post-create.d/`); see
  `scripts/README.md` and "Instance extension points" below.
- `Makefile` — the release contract `release.yml` drives, implemented by
  template-docker for the app image and tuned by `image.mk` — see "Release:
  a Makefile contract" and "Image layer (template-docker)" below.
- `Dockerfile`, `image.mk`, `.dockerignore`, `scripts/image-smoke.sh`,
  `scripts/runner-setup.sh`, `scripts/runner-setup.d/`,
  `scripts/check-dockerfile-versions.sh` — the image layer; see "Image
  layer (template-docker)" below.
- `compose.yml` — runner-image smoke-test stack (distinct from
  `.devcontainer/compose.yml`); see `.github/workflows/README.md`'s
  `smoke.yml` entry.
- `pyproject.toml` / `uv.lock` — dependencies, managed with `uv`, pinned
  to exact patch versions.
- `.pre-commit-config.yaml` — git hooks, run by `prek` or `pre-commit`:
  template-base's and template-docker's language-agnostic hooks plus this
  template's Python checks.
- `CLAUDE.md` — the AI-assisted coding workflow Claude Code follows in
  this repository; general conventions live in this file and each
  directory's own `README.md` instead.

## Instance extension points

Base-owned files stay `replace` tier (a sync overwrites them) and expose
hooks an instance fills with files it owns, so an instance never edits a
base-owned file directly. Don't edit `.devcontainer/Dockerfile`, `scripts/develop.sh`
or `compose.yml` in an instance; use the hooks below.

| Hook | Base side (template-owned) | Instance side (instance-owned) |
|---|---|---|
| Extra devcontainer tooling | `.devcontainer/Dockerfile` `develop` stage runs every `scripts/develop.d/*.sh` after `develop.sh` | `scripts/develop.d/NN-name.sh` |
| Extra post-create steps | `scripts/post-create.sh`, run by `devcontainer.json`'s `postCreateCommand`, runs every `scripts/post-create.d/*.sh` | `scripts/post-create.d/NN-name.sh` |
| Extra compose services / dev-service settings | `devcontainer.json` `"dockerComposeFile": ["compose.yml", "compose.instance.yml"]` | `.devcontainer/compose.instance.yml` (base ships a stub, `ignore` tier) |
| Instance skills and agents | base's manifest lists each of its own skills (`.claude/skills/<name>/`) and agents (`.claude/agents/<name>.md`) one by one under `replace`, not as a directory glob; a deleted base skill or agent keeps its line as a tombstone so sync removes it from instances. An instance's own name must not reuse a base or tombstoned one, since sync would overwrite or remove it | `.claude/skills/<name>/`, `.claude/agents/<name>.md`, listed in `.github/template-sync-manifest.local.yml`'s `ignore:` |
| Runtime image | nothing (the devcontainer image lives in `.devcontainer/`) | top-level `Dockerfile` + `.dockerignore`, listed in `.github/template-sync-manifest.local.yml`'s `ignore:` |

Files with no include mechanism of their own (JSON configs,
`.pre-commit-config.yaml` — prek has no include) are `merge` tier, so an
instance's additions survive a sync. The runtime image is the
top-level `Dockerfile` with a plain `.dockerignore`, both instance-owned:
base keeps the devcontainer image and its excludes under
`.devcontainer/`, where BuildKit picks up
`.devcontainer/Dockerfile.dockerignore` for that build automatically.

**One-time upgrade (instances synced from before this layout).** A sync
adds `.devcontainer/Dockerfile` but can't delete the old top-level
`Dockerfile`/`.dockerignore`: from this release on, base's manifest no
longer classifies those paths, and a merge-tier file is never deleted
automatically. In the sync PR, by hand:

- With a runtime image: rename `app.Dockerfile` → `Dockerfile` and
  `app.Dockerfile.dockerignore` → `.dockerignore`, overwriting the old
  base files. Update every reference (`-f app.Dockerfile` in the
  `Makefile`, `dockerfile:` in compose files, CI, docs) and list both
  paths under `ignore:` in `.github/template-sync-manifest.local.yml`.
- Without one: delete the old top-level `Dockerfile` and `.dockerignore`.

Instances of template-fastapi have nothing to do by hand: template-fastapi's
own manifest lists `Dockerfile` as `replace` (and `.dockerignore` as
`merge`) and keeps the retired `app.Dockerfile`/`app.Dockerfile.dockerignore`
paths listed, so one sync installs the new files and deletes the old ones.

## Getting started

1. Open this folder in a devcontainer (VS Code: "Reopen in Container" —
   `.vscode/extensions.json` recommends the extension that offers this —
   or any tool that reads `.devcontainer/devcontainer.json`). This starts
   the app alongside Postgres, RustFS (S3), Redis, MQTT, Keycloak (OIDC),
   and a Selenium container Playwright drives remotely for e2e tests;
   builds the `develop` stage; and installs dependencies and the git hooks
   via `postCreateCommand` (`scripts/post-create.sh`).
2. Run the app: `uvicorn app.main:app --reload --host 0.0.0.0`, or use
   the "FastAPI: api" launch config to run it under the debugger. Startup
   applies any pending Alembic migrations automatically — see
   `src/app/README.md`'s "Alembic migrations".
3. Health check: `curl localhost:8000/health/live` (liveness) or
   `curl localhost:8000/health/ready` (readiness — checks Postgres,
   Redis, S3, and the OIDC provider). `curl localhost:8000/crud/v1/heroes/v2/json`
   is a worked example CRUD resource (see `src/app/README.md`'s "Example
   CRUD resource: Hero"). `/protected` needs a bearer token from Keycloak —
   see `.devcontainer/stack/keycloak/README.md`.

Without a devcontainer: install [`uv`](https://docs.astral.sh/uv/) and
[`prek`](https://prek.j178.dev/), export your own `DATABASE_URL` /
`S3_ENDPOINT_URL` / `S3_ACCESS_KEY` / `S3_SECRET_KEY` / `REDIS_URL` /
`OIDC_ISSUER` (or the legacy `OIDC_ISSUER_URL`) / `OIDC_DISCOVERY_URL` /
`OIDC_AUTHORIZATION_URL` / `OIDC_TOKEN_URL` / `OIDC_CLIENT_ID` /
`OIDC_AUDIENCE` / `PUBLIC_BASE_URL`, and optionally `SECURITY_TXT_CONTACT`
(+ `SECURITY_TXT_EXPIRES`) (see `src/app/config.py` for the defaults), then
`uv sync --extra dev` and run uvicorn the same way. Run `prek install`
once to enable the git hooks.

## Checks

`ruff` (lint + format), `mypy --strict`, `pytest`, and `pip-audit`
(dependency vulnerabilities, run against exactly what `uv.lock` resolves)
are all configured to fail on any violation — see `pyproject.toml`.
`pytest` also fails below 100% coverage of `src/app`, for both the
default run (`tests/unit` + `tests/integration`) and `uv run pytest
tests/e2e`. `.github/renovate.json` opens a weekly update PR for Python
dependencies, GitHub Actions, and every Dockerfile/compose image tag. Run
everything at once with:

```bash
prek run --all-files --hook-stage manual
```

If a rule produces a false positive, silence that one line with a
justified `# noqa: <code>` rather than loosening the project-wide
configuration. Every function (including tests, `__init__`, and dunder
methods) gets a one-line docstring stating what it does — ruff's
`D100`–`D107` enforce this; see "Code style" below for what the
docstring should actually say.

Hooks are defined once in `.pre-commit-config.yaml` and run by
[`prek`](https://prek.j178.dev/) (a faster, drop-in-compatible
reimplementation of `pre-commit`, already a `dev` dependency — the
classic `pre-commit` CLI reads the same config file if you prefer it).
Fast, basic checks run on commit; slower, extensive checks (mypy,
pytest, `uv lock --check`) run on push. Every hook except the
commit-message check also carries the `manual` stage, so the command
above runs everything else at once. `.vscode/settings.json`'s
`git.commandsToLog` surfaces a failing hook's output in VS Code's Git
output channel immediately, instead of only on request.

Commit messages are checked separately: every commit message must
follow [Conventional Commits](https://www.conventionalcommits.org/),
enforced by the `conventional-pre-commit` git hook at the `commit-msg`
stage — an improperly formatted message is rejected before the commit
is created. This can't be run via the `manual` stage like the other
hooks (see the comment in `.pre-commit-config.yaml`); the only way to
check it is to actually commit.

CI (`.github/workflows/checks.yml`) runs the same `--hook-stage manual`
command, inside the devcontainer itself, on every push and pull request
— as an `amd64`/`arm64` matrix, both legs native (no QEMU). `smoke.yml`
runs the same matrix against the built `runner` image itself.
`.github/workflows/release.yml` is triggered manually to cut an
alpha/beta/rc/full release — see "Release: a Makefile contract" below and
`.github/workflows/README.md`. `.github/workflows/perf.yml` runs
the `tests/perf/` Locust load test against the `runner` image on
`workflow_dispatch` and a weekly schedule (not per-PR — see
`docs/adrs/0010-locust-for-load-testing.md`).

## Release: a Makefile contract

`release.yml` computes the next SemVer tag (`compute_next_version.py`,
unchanged across every instance), then drives these targets:

| Target | Runs | Where | Env available |
|---|---|---|---|
| `release-arches` | once, first | host runner | none; must print a JSON array on stdout, e.g. `["amd64"]` |
| `build`, `sbom`, `release-assets`, `publish` | once **per arch**, in that order | host runner for that arch | `RELEASE_VERSION`, `RELEASE_TAG`, `RELEASE_ARCH`, `OCI_REGISTRY`, `OCI_IMAGE_NAME`, `OCI_REGISTRY_USERNAME`, `OCI_REGISTRY_PASSWORD`, all `GITHUB_*`; no package-registry variables or secrets, and no OIDC token |
| `release-check` | once, after all arches; gates the GitHub release and `publish-packages` (not the per-arch image `publish`, which already ran in `build`) | **inside the devcontainer** (`devcontainers/ci`), stack up | `RELEASE_VERSION`, `RELEASE_TAG`, `GITHUB_REPOSITORY`; **not** `RELEASE_ARCH` (it runs once, not per arch) and no `ARCHES`: to check each arch's files in `dist/`, iterate over `make -s release-arches` |
| `finalize` | once, after `release-check`; then the GitHub release is created | host runner | same as per-arch minus `RELEASE_ARCH` |
| `publish-packages` | once, after the GitHub release exists, **only if** `PYPI_PUBLISH`, `NPM_PUBLISH` or `CARGO_PUBLISH` is set | host runner (own job, with `id-token: write`) | `RELEASE_VERSION`, `RELEASE_TAG`, `PYPI_PUBLISH`, `NPM_PUBLISH`, `CARGO_PUBLISH`, all `GITHUB_*`, the OIDC token, `NPM_TOKEN` and `CARGO_REGISTRY_TOKEN` (bootstrap secrets; empty when unset, so treat empty as absent); `dist/` holds every arch leg's files |
| `verify-published` | once, after `publish-packages` succeeded, **only if** a `*_PUBLISH` variable is set | host runner (own job, no secrets, no OIDC token) | `RELEASE_VERSION`, `RELEASE_TAG`, `PYPI_PUBLISH`, `NPM_PUBLISH`, `CARGO_PUBLISH`; `dist/` as for `publish-packages` |

What each target is for: `build` produces the release artifact(s) (an
image, `cargo build --release`, `npm pack`, `helm package`, ...); `sbom`
writes an SBOM for what `build` produced; `release-assets` populates
`dist/` (gitignored) with every file to attach to the GitHub release;
`publish` pushes the per-arch image to its registry, skipping cleanly when
`OCI_REGISTRY` isn't set; `publish-packages` uploads packages (PyPI, npm,
crates.io) once per release, never once per arch, and runs in a job that
exists only for instances that set one of the `*_PUBLISH` variables, so
an image-only instance never has `id-token: write` or a package secret
(base's default fails if any `*_PUBLISH` variable is set, so an instance
can't silently skip it; with none set it is a no-op; it runs on a fresh runner with a checkout and the
downloaded `dist/` only, so it uploads from `dist/` and sets up its own
tooling); `verify-published` checks the release from the registry (e.g.
installs the just-published package) and is a no-op in base's default, since
there is nothing to verify unless an instance implements it;
`release-check` runs checks that need the backing services (e.g. tests
producing a coverage report) and the local artifact; `finalize`
does cross-arch work such as combining per-arch registry tags into one
multi-arch manifest list. `RELEASE_VERSION` (e.g. `1.2.3` or
`1.2.3-alpha.1`) and `RELEASE_TAG` (the same, `v`-prefixed) are for
embedding or tagging with the version. A single-arch no-op is the
default: `release-arches` prints `["amd64"]`.

**Upgrading a package instance.** Before this contract, `publish` also
uploaded packages, once per arch. Now `publish` is image-only and gets no
`*_PUBLISH` variables, secrets or OIDC token. An instance that publishes
packages must move that upload from `publish` into a new
`publish-packages` target (reading artifacts from `dist/`, setting up its
own tooling). Until it does, base's default `publish-packages` (if the
instance kept it) fails with a message naming the variable that is set;
an instance whose Makefile has no such target fails with `make: No rule
to make target`. Either way nothing is uploaded, and because the GitHub
release is created first (see "Job order"), the release already exists
when the `publish-packages` job fails: it is out without its packages.
Fix the Makefile and re-run the failed jobs to publish them; do not cut
another release for it.

**Job order.** `build` (per arch) → `release-check` → `release`
(`finalize`, then the GitHub release) → `publish-packages` →
`verify-published`. `release-check` gates both the release and package publishing. It does
not gate the per-arch image `publish`, which runs inside `build`, so a pushed
per-arch image can exist for a release that `release-check` then fails.
The GitHub release exists before any registry upload, so a partial
registry failure leaves the release in place and a re-run of the failed
job finishes publishing. So an instance's `publish-packages` **must be
safe to run again**: it skips versions already on the registry (for
example `uv publish --check-url`, or an `npm view` guard) instead of failing
on them or double-publishing.

The `release` job is re-runnable too: if the release already exists it
skips `finalize` and the release creation (a leftover draft is deleted and
recreated), so re-running failed jobs after a later job failed does not
fail on the existing release. It first checks that the existing
release's tag points at the commit this run built, and fails (rather than
warns) if the tag can't be resolved or points elsewhere
(`.github/scripts/verify_release_tag.sh`). The tag lookup is tried up to six
times over about two minutes (waits of 5, 10, 20, 40 and 60 seconds, enough
for a minute-long secondary rate limit), but only for a transient failure
(HTTP 5xx, 408 or 429, a rate limit, or a network error). A tag that does not
resolve (the API answers 404, or 422 "No commit found") or any other permanent
error fails at once. The 404 message names both causes: for a deleted tag,
restore it or delete the release and run the workflow again; for a release
that was only just created (say by a concurrent run) whose tag is not visible
yet, re-run the job. An API answer without a commit sha is reported as such,
not as an unresolvable tag. If `release` itself failed before the release
existed, `finalize` runs again on the re-run, so an instance's `finalize`
must be safe to repeat (it must not double-apply its side effects).
Because the release is created before `publish-packages` runs, a GitHub
release can exist for a version whose `publish-packages` failed. Re-run the
failed jobs to finish publishing; do not delete the release.
`verify-published` runs only after `publish-packages` succeeded, so after a
partial `publish-packages` failure nothing is verified until the re-run of
the failed jobs, which then runs it.

Two runs for the same tag cannot overlap in the `release` job: it sits in a
`concurrency` group named after the tag (`cancel-in-progress: false`), so the
second waits and then sees the published release. If that release was cut
from a different commit than the waiting run's (two dispatches that computed
the same tag), the waiting run fails instead of publishing its own `dist/`
under that version (detected only here, after the per-arch images of both runs were
pushed). GitHub keeps at most one waiting run per group, and a
newer waiting run replaces it.

A release that exists on GitHub counts as complete: whatever failed after it
is finished by re-running the failed jobs, never by cutting another version.

**Opting into a registry.** The `PYPI_PUBLISH` (`testpypi` or `pypi`),
`NPM_PUBLISH` and `CARGO_PUBLISH` repository variables are what opt an
instance into a registry; with none set, `publish-packages` and
`verify-published` do not run. `NPM_TOKEN` and `CARGO_REGISTRY_TOKEN` are
only for the first release of a package, before trusted publishing is set up
at the registry. See `.github/workflows/README.md`,
"Release variables and secrets".

Re-run only the *failed jobs* of a run. "Re-run all jobs" runs `version`
again, which sees the tag the first attempt created and computes the next
version, so it cuts another release instead of finishing the first.

In `release-check`, if `dist/` holds files owned by another user (the
devcontainer ran as root), the job takes them over with `sudo`, which must
be passwordless (`sudo -n`); the runner images provide that. A self-hosted
runner without it fails there with a message saying so.

Package templates (npm, Python, Rust) move their
registry check ("install what was just published") from `release-check` to
`verify-published`; their `publish-packages` is unchanged and already skips
versions already on the registry. Base's
`release-check` no longer receives the `*_PUBLISH` variables.

**Package instance checklist.** An instance that publishes a package also:
sets `repository`, homepage and bugs URLs in its package manifest
(`package.json`, `pyproject.toml`, `Cargo.toml`) -- npm provenance
verifies the repository URL against the publishing repository, and the
other registries show these links to users; and implements `publish-packages`
(re-runnable: it skips versions already on the registry) in its own `Makefile`, which replaces base's default (the
`DefaultPublishPackagesTest` in `.github/scripts` skips itself once the
default message is gone). That only works where the `Makefile` is
instance-owned, as in base. A downstream template that makes the `Makefile`
replace-tier (an image template, say) gives its instances no place to
implement it, so such a template either ships the `publish-packages` logic
itself, driven by variables the instance sets, or is not a fit for package
instances. `release-check` receives
`GITHUB_REPOSITORY`, so an instance can run repository guards there.
It also moves any "install what was just published" check from
`release-check` to `verify-published` (see "Job order" above).

`dist/` rules: each arch leg's `dist/` is uploaded as artifact
`release-assets-<arch>` (a tarball, so it is never empty), and
`release-check`'s job assembles all of them into one `dist/`, failing if an
arch's upload is missing or two legs produced the same file name, so
**per-arch file names must include the arch**. `release-check` may add
more files to `dist/`, and the assembled `dist/` (uploaded at the end of the
`release-check` job, before `finalize`) is what `release`,
`publish-packages` and `verify-published` start from. `finalize` may add
more files, but they are attached to the GitHub release only:
`publish-packages` and `verify-published` do not see them. A path that two
legs both produce (the same file name, a file in one and a directory of
that name in another, or a symlink) fails the assembly. The assembly also
refuses to run when `dist/` already exists and is not empty, so stray files
cannot end up in a release.
`release.yml` then runs `gh release create` with every file left in
`dist/`.

The arch legs run natively, not under QEMU: `arm64` on `ubuntu-24.04-arm`,
`amd64` on `ubuntu-24.04`, each overridable with the `CI_RUNNER_ARM64` /
`CI_RUNNER_AMD64` repository/organization variables.

This repo's `Makefile` is template-docker's (see "Image layer
(template-docker)" below), tuned by `image.mk`: `release-arches` prints
both `amd64` and `arm64`; `build` builds `Dockerfile`'s `runner` stage to a
per-arch OCI tarball; `sbom` writes its SPDX SBOM with Syft; `publish`
copies that same tested tarball to a per-arch registry tag (when
`OCI_REGISTRY` is set); `release-check` runs the image smoke test plus
`image.mk`'s `stack-smoke` (the real-services `/health/ready` smoke, see
NFR-0010) and `pytest` for `coverage.xml`; and `finalize` combines the
per-arch tags into one multi-arch manifest list.

## Image layer (template-docker)

`template-docker` is a mid-layer template: an instance of `template-base`
that is itself the template for every app that ships a container image
(`template-fastapi`, `template-axum`, `template-react`). Sync runs in
three hops, base to docker to app, and `ADR 0001` records why. It adds
these files over base:

| Path | Tier for instances | What it is |
|---|---|---|
| `Makefile` | `replace` | the image implementation of the release contract above |
| `image.mk` | `ignore` | the instance's settings for the Makefile, every variable commented |
| `Dockerfile` | `ignore` | skeleton to replace: `builder` and `runner` stages, `ARG DEBIAN_VERSION`, `ARG APP_UID`, SSL ARGs, `APP_VERSION`/`APP_COMMIT` |
| `.dockerignore` | `merge` | baseline build-context excludes for the runtime image (including `node_modules`, `coverage`, `target` and `dist`); the instance appends its own |
| `scripts/image-smoke.sh` | `replace` | loads the image tar from `dist/`, runs it and polls a URL |
| `scripts/runner-setup.sh` | `replace` | optional: creates the group-0 runtime user and its writable directories for the `runner` stage |
| `scripts/image-scan.sh` | `replace` | the Trivy scan the `image-scan` hook runs |
| `scripts/test-image-scripts.sh` | `replace` | offline tests of the Makefile and `scripts/` logic, run by the `image-scripts-test` hook |
| `scripts/runner-setup.d/*.sh` | `ignore` | optional: the instance's runner-stage hooks |
| `scripts/check-dockerfile-versions.sh` | `replace` | the version-ARG consistency check |

### Package targets

The image Makefile ships `publish-packages` and `verify-published` so an
image instance satisfies the contract above (base's
`MakefileDefinesPublishPackagesTest` requires a `publish-packages:` rule;
without one a release with a `*_PUBLISH` variable set stops with `No rule to
make target`). Both mirror base's defaults. `publish-packages` is a no-op
unless `PYPI_PUBLISH`, `NPM_PUBLISH` or `CARGO_PUBLISH` is set, in which
case it exits non-zero naming the variable, so an image-only instance can't
silently skip a package upload it configured. `verify-published` is a
no-op. The Makefile is `replace` tier, so an instance cannot override these
targets locally (a sync would overwrite the change). Base's "Package
instance checklist" has the instance implement `publish-packages` in its own
`Makefile`, which only works where the `Makefile` is instance-owned; here it
is not, so an image instance that also publishes a package cannot do that.
The upload and the registry check have to be added to template-docker first
(keyed on variables the instance sets), then the instance re-syncs.

### `image.mk` variables

The Makefile starts with `-include image.mk` (a missing file means all
defaults) and every variable is `?=`, so set them in `image.mk`:

| Variable | Default | Meaning |
|---|---|---|
| `DOCKERFILE`, `TARGET` | `Dockerfile`, `runner` | build file and stage |
| `ARCHES` | `amd64 arm64` | space-separated; `release-arches` prints it as a JSON array and `finalize` merges the per-arch images into one tag; must not be empty |
| `BUILD_ARGS` | empty | full flag words, e.g. `--build-arg APP_VERSION=$(RELEASE_VERSION)` |
| `EXTRA_LABELS` | empty | full flag words, e.g. `--label k=v` |
| `OCI_DESCRIPTION` | empty | `org.opencontainers.image.description`; the label is omitted while empty |
| `OCI_LICENSE` | `GPL-3.0-only` | SPDX id for `org.opencontainers.image.licenses` |
| `COVERAGE_REPORT` | empty | release asset name for the `io.github.<owner>.coverage-report` label; omitted while empty |
| `IMAGE_SMOKE_PORT`, `IMAGE_SMOKE_PATH` | `8000`, `/health/live` | URL polled for a 2xx |
| `IMAGE_SMOKE_RUN_ARGS`, `IMAGE_SMOKE_TIMEOUT` | empty, `60` | extra `docker run` words, whitespace-separated with no embedded spaces or quotes (passed through the environment); seconds to wait |

`SYFT_VERSION` (the SBOM tool image tag) and `SKOPEO_IMAGE` (tag and
digest) are pinned in the Makefile for Renovate; don't override them. The
image name comes from `GITHUB_REPOSITORY` (the repository name), never a
hardcoded one. Inside the devcontainer, where `release-check` runs and
`GITHUB_REPOSITORY` is unset, it is derived from the `origin` remote
(`ssh://[user@]host[:PORT]/owner/repo`, scp-like or https form; tested by
`scripts/test-image-scripts.sh`). If git fails (no `origin`, "dubious
ownership"), there is no guess: `build`, `sbom`, `release-assets`, `publish`
and `finalize` print git's error to stderr and fail, and
`GITHUB_REPOSITORY=owner/repo` overrides;
`image-smoke` also accepts the single `dist/*-<version>-<arch>.tar` when
the expected name is absent. `OCI_IMAGE_NAME` overrides the registry path.
`build`, `sbom`, `release-assets` and `publish` need `RELEASE_ARCH` and
fail through the `check-arch` prerequisite when it is empty;
`release-arches`, `release-check`/`image-smoke` (host arch when unset) and
`finalize` don't. `publish` copies the tar from `dist/` that was SBOM'd
and smoke-tested to the registry with skopeo (`copy --all`, `oci-archive:`
to `docker://`, Apache-2.0, pinned image; `crane push` only reads
docker-format tarballs, and `build` writes an OCI layout), so the pushed
digest is the tested one. `--all` pushes the tar's whole index as
`<version>-<arch>`: the image for `RELEASE_ARCH` plus the buildx attestation
manifests (provenance, SBOM) when the build wrote any. Without it skopeo
picks one image by the *host's* platform, which can fail or pick the wrong
image when `RELEASE_ARCH` isn't native, and it drops the attestations. It
records that index's digest in `dist/<name>-<version>-<arch>.digest` (which
`finalize` deletes after use, so release.yml never attaches digest files to
the GitHub release), and
`finalize` builds the manifest list from those digests (`imagetools create`
merges the per-arch indexes, so the attestations carry over). `finalize`
logs in with a throwaway `DOCKER_CONFIG`, into which it links the user's
`cli-plugins` directory (Docker looks up plugins in
`$DOCKER_CONFIG/cli-plugins`), so a user-installed buildx is still found. Registry login uses a throwaway authfile fed the
password on stdin. The labels are listed in `.github/workflows/README.md`'s "Image
labels". Registry auth stays the opt-in `OCI_REGISTRY*` secrets contract.

`finalize` must be safe to run again, and this one is: base's `release` job
re-runs it when an earlier run failed before the GitHub release existed.
`imagetools create` re-points the same tag at the same digests, and the
digest files it reads are still in the `dist/` the re-run downloads, because
that `dist/` was uploaded before the first `finalize` deleted them. Any step
added to `finalize` here must keep that property (no side effect that
double-applies).

### Adding `release-check` steps

`release-check` has no recipe of its own: it depends on `image-smoke`.
An instance adds steps as prerequisites in `image.mk`, so Make never
sees two recipes for one target:

```make
release-check: instance-tests
instance-tests:
	uv run pytest --cov-report=xml:dist/coverage.xml
```

`release-assets` depends on `image-smoke`, so each arch is booted
natively on its own runner in the build job before `publish`; the
`release-check` run inside the devcontainer smoke-tests the host arch
again with the backing services up. `image-smoke` converts the arch's
image in the OCI-layout tar with the pinned skopeo (through
`scripts/skopeo.sh`, which `publish` uses too; `--override-arch`, to a
docker-archive under `dist/`) and `docker load`s that, because
`docker load` can't read a buildx OCI tar on the classic image store,
with or without attestations. It reads `IMAGE_TAR_PREFIX`,
`SKOPEO_IMAGE` and `ARCHES` (set by the Makefile), the arch from
`RELEASE_ARCH` (else the host's). A missing tar for that arch fails, so a
run never passes without having tested an image; the one exception is
`RELEASE_ARCH` unset with a host arch outside `ARCHES` (the instance
doesn't build it, and its own build leg tested each arch it does). It maps
a random host port to `IMAGE_SMOKE_PORT`, prints the container logs on failure and always
removes the container and the private `image-smoke-*` tag it created
(never an image that existed before).

### Runner setup hook

Optional. It serves a runtime image that needs the group-0 user and
writable directories; an instance whose `runner` stage doesn't call it
(e.g. `template-react`'s nginx runner, whose base image already has its own
non-root user) simply never runs it. Both files stay in the tree, since
they are template-owned, and nothing checks or requires them.

`scripts/runner-setup.sh` creates the user (UID `APP_UID`, group 0), makes
its home and `/app/data` group-writable, and leaves `/app` root-owned, so
the image also runs under an arbitrary UID (OpenShift's restricted SCC).
Run it **before** copying app code, then copy the code after it with
`COPY --chown=<uid>:0` of content whose modes are already 0550
directories and 0440 files: only data and cache directories are writable, the runtime user
can't modify what it runs, and no recursive `chgrp`/`chmod` duplicates
the files into another layer. Set those modes numerically, per kind
(`find . -type d -exec chmod 0550 {} +`, `find . -type f -exec chmod 0440
{} +`, e.g. in the builder stage, or `COPY --chmod=0550`/`0440` for sources
that are all one kind), not with the symbolic `--chmod=u=rX,g=rX,o=`: that
form left files unreadable to the app user on GitHub's stock-runner Docker
(template-fastapi's CI smoke, even with `# syntax=docker/dockerfile:1.28`),
though it worked on a current local daemon. It then runs every
`scripts/runner-setup.d/*.sh` in glob order as `bash NN-name.sh APP_UID`,
still as root and in the same `RUN` layer. The instance puts extra
packages and writable directories there instead of editing the script.

### Dockerfile version check

The `check-dockerfile-versions` hook runs
`scripts/check-dockerfile-versions.sh DEBIAN_VERSION` whenever either
Dockerfile, the script or `.pre-commit-config.yaml` (where the ARG list
lives) changes: each named ARG must have the same default in
`.devcontainer/Dockerfile` and `Dockerfile` (the runtime must match the
toolchain the devcontainer tests against). An ARG may be declared several
times in one file (a bare `ARG NAME` re-declares it inside a stage); bare
ones are ignored and every default given must agree. An instance appends the ARG
names it keeps in both files (e.g. `RUST_VERSION`) to the hook's `entry:`
in its `.pre-commit-config.yaml`; ARGs not listed aren't checked.

### Image hooks

- `hadolint` lints both Dockerfiles, run from the pinned official image
  (so it needs Docker). It fails on warnings and above, not on info-level
  findings, and has no repo-wide ignores. An instance that must
  install an unpinned apt package adds a per-line
  `# hadolint ignore=DL3008` with a reason (as the skeleton `Dockerfile`
  does: Debian drops superseded versions, so pins break rebuilds).
- `image-scripts-test` runs `scripts/test-image-scripts.sh`: offline tests
  of the Makefile's repo derivation, the version check, the smoke test's
  failure on a missing tar and `skopeo.sh`'s argument handling. It needs no
  Docker.
- `image-scan` runs only at the `manual` stage, because it needs
  the tar from `make build` and network access for the vulnerability
  database. It scans only `dist/*-<RELEASE_VERSION>-<arch>.tar` (arch from
  `RELEASE_ARCH`, else the host's; without `RELEASE_VERSION`, the newest
  `dist/*-<arch>.tar`), never stale tars of other versions. With no such tar
  it skips. Otherwise `scripts/image-scan.sh` runs a
  pinned Trivy image on that tar and fails on HIGH or CRITICAL
  findings that have a fix (`--severity HIGH,CRITICAL --ignore-unfixed
  --exit-code 1`). Trivy can't read the OCI-layout tar `make build`
  writes, so the script unpacks it and scans the directory, selecting
  `linux/<arch>` from the tar's name so a buildx attestation manifest is
  never scanned instead of the image. The database is cached in the `trivy-cache` Docker
  volume. Trivy is Apache-2.0, so invoking it as a tool is compatible
  with this repo's GPL-3.0 licence. The scanner is the maintainers'
  choice, made over Grype. Trivy's releases and Docker Hub images were
  compromised in March 2026 (GHSA-69fq-xp46-6x23), so the image is pinned
  by digest as well as tag (`aquasec/trivy:<version>@sha256:…`); the
  advisory lists images referenced by digest as unaffected. Renovate
  updates tag and digest together. When bumping by hand, take the digest
  from the registry and verify it with cosign as the advisory describes:
  `cosign verify --certificate-identity-regexp
  'https://github\.com/aquasecurity/' --certificate-oidc-issuer
  'https://token.actions.githubusercontent.com'
  ghcr.io/aquasecurity/trivy@sha256:…` (the Docker Hub, GHCR and ECR
  digests of a release are identical).

### Re-parenting an existing app onto template-docker

One-time steps for an app that is today an instance of `template-base`
(or of a sibling template) and moves under `template-docker`:

1. Make the tracked files equal `template-docker`'s tag: take the
   template's `Makefile`, `scripts/` files, `.dockerignore` and
   `.pre-commit-config.yaml` hooks, move the app's Makefile settings into
   `image.mk` and its runtime user setup into `scripts/runner-setup.d/`,
   and list its version ARGs on the `check-dockerfile-versions` hook.
2. For `template-react` and `template-axum`, which were not synced from
   base, migrate to base's layout first (devcontainer, workflows, docs,
   manifest) and let a base sync PR settle before this step.
3. Bootstrap the sync state: run `template-sync.yml` manually with
   `template_repo=craftainer/template-docker` and `initial_sync_tag` set
   to the tag the files now equal. That writes
   `.github/template-sync-state.json` without changing files. From then on
   syncs track `template-docker`, which in turn tracks base.
4. Resolve base's additions to the manifest by hand when
   `template-docker` is the one syncing from base, as the header of
   `.github/template-sync-manifest.yml` says.

## Template sync

Once instantiated, a repo created from this template can pull in later
template fixes/improvements via `.github/workflows/template-sync.yml`
(itself template-owned, `replace`-tier): on a schedule (cadence set by
the `TEMPLATE_SYNC_INTERVAL` repository variable, `weekly` or `monthly`)
or on demand, it diffs the instance against the template's latest tagged
release, per `.github/template-sync-manifest.yml`'s three tiers
(`replace`, `ignore`, `merge` — see that file's header), and opens a PR
with the result. It never pushes directly or auto-merges; a genuine
`merge`-tier conflict is left with `<<<<<<<` markers for a human to
resolve. An instance that predates this workflow bootstraps its
`.github/template-sync-state.json` via the workflow's manual
`initial_sync_tag`/`template_repo` inputs first.

The workflow runs `template_sync.py apply` from the *new* tag's checkout,
with that tag's manifest. Do the same when applying a sync by hand: the
instance's own copy of the script is the old release's, and an older one
does not expand glob entries such as `scripts/release-*.sh`, so it skips
those files (a second run with the new script then picks them up). `apply`
is meant to run once per tag pair: the state file moves to the new tag
afterwards. Running it again is harmless for files already merged cleanly,
but it merges a hand-resolved conflict again and re-creates its markers.

Further sync behaviour, detailed in `.github/scripts/README.md`:

- `apply` is not atomic. CI discards the checkout and does not advance the
  state on an error; after a failed manual run, reset the working tree with
  git before trying again.
- A binary `merge`-tier file the instance changed cannot be merged. It is
  kept as it is, reported as a conflict, listed as `merge.binary` in the
  summary and labelled in the sync PR.
- The moderation `-apply` workflows fail on an expired `moderation-result`
  artifact instead of passing: a produced result was lost, so re-run the
  worker run (see `.github/workflows/README.md`, "Issue moderation").
- The scripts under `.github/scripts/` run on the runner's system `python3`.
  Base ships `.github/scripts/ruff.toml` (`replace` tier), pinned to that
  Python's `target-version`, and `test_script_syntax.py` guards it with
  `ast.parse(feature_version=...)`. A Python instance must not override or
  remove it: its own `pyproject.toml` targets a newer Python and ruff would
  rewrite those scripts into syntax the runner cannot parse.

An instance that isn't itself a template shouldn't edit that manifest to
list its own files: it's `merge`-tier, so edits would conflict with later
syncs. It lists them under an `ignore:` key in the instance-owned
`.github/template-sync-manifest.local.yml` instead — including any extra
`scripts/develop.d/`/`scripts/post-create.d/`/`scripts/runner-setup.d/`
hook scripts of its own (the template's own `10-remove-pip.sh` is
`replace` tier); the
`template-sync-manifest` check merges that file in, but sync never reads
it, and it accepts no other tier.

`template-fastapi` is itself both an instance of
[`template-docker`](https://github.com/craftainer/template-docker) *and* a
template for further instances: its `.github/template-sync-manifest.yml`
classifies its *own* tracked files for *its* downstream instances,
separate from template-docker's manifest (it edits the manifest directly
and resolves docker's additions by hand). A generic scaffolding fix
therefore reaches a template-fastapi instance in three hops: template-base
into template-docker, template-docker into template-fastapi, then
template-fastapi into the instance.

An instance created from template-fastapi before `v0.0.1-alpha.1` can't
take its first sync unaided: its `template-sync.yml` pushes the sync
branch with the default `GITHUB_TOKEN`, which GitHub refuses for any
change under `.github/workflows/`, and that sync always includes some —
among them the fixed `template-sync.yml` itself. Upgrade such an
instance by hand, once:

1. Copy template-fastapi's current `.github/workflows/template-sync.yml`
   over the instance's own, and merge that change.
2. Add a `TEMPLATE_SYNC_TOKEN` secret with workflow write access (see
   `.github/workflows/README.md`'s "Template sync").
3. Run Template sync again; from then on it syncs unaided.

## Template family

`template-base` is the root of a small family. Four mid-layer templates sit
under it, and each library or app is an instance of exactly one of them (the
three app templates through `template-docker`):

```text
template-base
├── template-python-package ── lib-crud-py, lib-observability-py, lib-crud-sdk-py
├── template-npm-package ───── lib-crud-sdk-ts, lib-crud-views-ts
├── template-rust-crate ────── lib-crud-rs, lib-observability-rs
└── template-docker
    ├── template-fastapi
    ├── template-axum
    └── template-react
```

Naming: a library repo is `lib-<name>-<py|ts|rs>` and an app repo is
`template-<runtime>`; the registry package is `craftainer-<name>` on PyPI and
crates.io and `@craftainer/<name>` on npm (`lib-crud-sdk-ts` publishes
`@craftainer/crud-sdk`). Libraries declare `GPL-3.0-only` in their package
manifest; base itself ships the GPL v3 text in `LICENSE`.

Each mid-layer template is an instance of base that is itself a template. It
adds one kind of artifact to base's release contract and keeps everything
else from base:

| Template | Adds to base | Tooling it owns (`replace`) | What an instance owns (`ignore`) |
|---|---|---|---|
| `template-docker` | the container-image release: multi-arch build, SBOM, OCI labels, smoke test, Trivy scan, Dockerfile version check, and `publish-packages`/`verify-published` as no-ops | `Makefile`, `scripts/image-*.sh`, `scripts/runner-setup.sh`, `scripts/check-dockerfile-versions.sh`, `scripts/skopeo.sh` | `Dockerfile`, `image.mk`, `scripts/runner-setup.d/*.sh` |
| `template-python-package` | a pure-Python library on PyPI (trusted publishing) | `Makefile`, `scripts/release-*.sh`, `scripts/post-create.d/10-uv-sync.sh`, `codspeed.yml` | `pyproject.toml`, `uv.lock`, `src/`, `tests/` |
| `template-npm-package` | an ESM TypeScript package on npm (staged first publish, then trusted publishing) | `scripts/release-*.sh`, `scripts/develop.d/10-corepack.sh`, `scripts/post-create.d/10-pnpm-install.sh` | `package.json`, `tsconfig*.json`, `tsdown.config.ts`, `vitest.config.ts`, `src/`, `tests/`, `pnpm-lock.yaml`, and the `Makefile` (a thin caller of the scripts) |
| `template-rust-crate` | a library crate on crates.io (trusted publishing, bootstrap-token fallback) | `Makefile`, `scripts/release-*`, `rust-toolchain.toml`, `rustfmt.toml`, the rustup and `cargo fetch` hooks | `Cargo.toml`, `Cargo.lock`, `src/`, `tests/`, `deny.toml` |

Each template's own `docs/TEMPLATE.md` has the details and the exact
manifest; its `docs/adrs/0001-*.md` records why it exists. The three app
templates inherit the image layer from `template-docker`; they are still
the place for their own runtime (FastAPI, axum, React) and their own ADRs.

**Sync tiers across the family.** Every hop uses the same three tiers
(`replace`, `ignore`, `merge`; see "Template sync" and the manifest header).
A mid-layer classifies its own tree for its instances in its own
`.github/template-sync-manifest.yml`, which is `merge` tier in every
instance: base's additions are resolved by hand there.
An instance lists extra paths of its own under `ignore:` in
`.github/template-sync-manifest.local.yml`. Which template an instance
follows is recorded in its `.github/template-sync-state.json`
(`template_repo`, `synced_tag`). An instance follows its immediate parent: a
package or image instance its mid-layer, and the mid-layer follows base.

**Order of a change.** A sync reads the parent's latest tagged release, so a
change travels one released tag at a time:

1. Fix it in `template-base`, merge, and release a new tag.
2. Each mid-layer takes the base release through a sync PR, merges it and
   releases its own tag. A base fix reaches `template-docker` and the
   three package templates this way; the app templates take it from
   `template-docker` in step 3, after it has released.
3. Every instance takes its parent's tag through its own sync PR (the app
   templates take `template-docker`'s; the libraries, their package
   template's).

A fix to a mid-layer's own files starts at step 2 and skips base. A change to
the release contract itself (a new target, a changed job order) is a minor
release of base, and each mid-layer then adapts its own implementation of the
target before instances re-sync; the "Job order" above is the current
result of two such changes. Instances that publish packages follow the
registry's own ordering on top: a package that depends on another is
released after it.

## Versions and config

Every version and config value is defined in exactly one place; nothing
duplicates or re-pins it elsewhere:

- Python/Debian base image versions: the `PYTHON_VERSION`/`DEBIAN_VERSION`
  `ARG` defaults at the top of `.devcontainer/Dockerfile` (`develop`) and
  `Dockerfile` (`builder`/`runner`), kept identical by
  `scripts/check-dockerfile-versions.sh`. `.devcontainer/compose.yml`
  does **not** pass matching `args:` — the Dockerfiles' own defaults are
  authoritative because they're closer to where they're used. To change
  them, edit both Dockerfiles, not the compose file. `PYTHON_VERSION`
  is a single exact-patch pin shared by all three stages: `builder`/`runner`
  use it directly as the `python:${PYTHON_VERSION}-slim-${DEBIAN_VERSION}`
  base image, while `develop` (based on the generic
  `mcr.microsoft.com/devcontainers/base` image, which carries no Python of
  its own) has `develop.sh` install that same version via `uv python
  install`, so all three stages run identical interpreters.
- Python package versions: `pyproject.toml` / `uv.lock`. Dependencies are
  managed with `uv` — use `uv add <package>` / `uv sync` rather than
  editing the dependency lists by hand or invoking `pip` directly.
  All development-only tooling — linters, type checker, test runners,
  Playwright — lives in the single `dev` optional-dependencies group
  rather than split across several groups, since it's all
  development-related; if a future group needs another group's packages,
  reference it as a self-referential extra (e.g. `"template-fastapi[dev]"`,
  per PEP 621) instead of re-pinning the same package a second time.
- `craftainer-crud` and `craftainer-observability` are published
  packages (PyPI), held to a `>=X.Y.Z,<X.(Y+1)` range in `pyproject.toml` and bumped by
  Renovate; their source, tests and import-linter contracts live in their own
  repositories, not under `src/`.
- Everything else pinned (base images, Actions, hook revisions, and the
  devcontainer's `prek`/Claude Code CLI/`snip`/`gh`/`uv`/Node.js):
  pinned once, at its single point of use, to an exact patch version —
  never a floating range or `latest` — so Renovate can bump them one at a
  time and the diff shows exactly what changed. Node.js (`NODE_VERSION`
  in `.devcontainer/Dockerfile`) is infrastructure tooling only — `npx`, for the
  `clear-thought` MCP server in `.mcp.json` — not part of this app's own
  runtime; `scripts/develop.sh` installs it directly from nodejs.org's
  release tarballs, checksum-verified, rather than a devcontainer
  feature, so it's pinned the same way as everything else in the
  `develop` stage.

### Developing a package and the app together

To change `craftainer-crud` or `craftainer-observability` while
running the app against your edits, point `uv` at a local checkout with an
editable path override in `pyproject.toml`:

```toml
[tool.uv.sources]
craftainer-crud = { path = "../lib-crud-py", editable = true }
craftainer-observability = { path = "../lib-observability-py", editable = true }
```

then run `uv sync` (not `--locked`: the override changes the lock). This is
local only and is never committed: `uv lock --check` in CI and the pinned
`==X.Y.Z` dependencies must stay the source of truth. Revert the override
(`git checkout pyproject.toml uv.lock`), release the package, and let Renovate
(or you) bump the pin when the work is done.

`ruff`, `mypy`, and `pytest` all point their cache dirs at
`/home/vscode/.cache/<tool>` (set once each, in `pyproject.toml`'s
`[tool.ruff]`/`[tool.mypy]`/`[tool.pytest.ini_options]`) instead of the
project root. Same reasoning as not bind-mounting the venv (see
`.devcontainer/README.md`'s "Don't" section): a cache inside the
bind-mounted `/workspace` gets scanned file-by-file by host
antivirus/malware tools on Windows and is painfully slow to write to. A
new tool with its own on-disk cache follows the same pattern.

## Code style

Every file gets a brief header stating what the file *is for* — one
line, sometimes two: a leading comment for most formats, a module
docstring for Python (also required by ruff's `D100`). Never describe
the file's contents in the header; that's what reading the file is for.
Markdown files' own title/opening line already serves this purpose. A
format with no comment syntax (`.json`) documents itself via the
directory's `README.md` instead.

A comment earns its place by saying something the code/config next to
it can't: *why* it's written this way, a non-obvious consequence, or a
constraint that isn't visible locally. Don't add a comment that just
restates what the following line already says in code — if removing a
comment loses no information, remove it. Every existing comment in this
repository follows this rule; keep new ones held to the same bar.

## Do

- Read `CLAUDE.md` and the `README.md` of every directory on the path to
  whatever you're changing before you change it.
- Open this repo in the devcontainer rather than assembling the
  toolchain by hand — it's the one environment this template guarantees.

## Don't

- Commit a `.env` file, or read one from application code — configuration
  lives in the compose files; secrets live in `.secrets/`.
- Add a `ports:` mapping or a `networks:` block to any file under
  `.devcontainer/` — see `.devcontainer/stack/README.md`'s "Devcontainer
  stack pattern" section for how host access and inter-service
  networking are handled instead.
