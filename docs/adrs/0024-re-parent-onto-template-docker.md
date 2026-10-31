# 0024. Re-parent onto template-docker and release through its image Makefile

## Status

Accepted

Note (2026-10-26, LIC1): the project licence identifier is now `GPL-3.0-only`, not `GPL-3.0-or-later`; the `OCI_LICENSE` default and the `licenses` label follow it. The `GPL-3.0-or-later` mentions below record what was decided at the time.

## Context

template-fastapi was an instance of `template-base` that carried its own
image plumbing: a release `Makefile` (build, SBOM, labels, publish,
manifest list), `scripts/runner-setup.sh`, a no-argument
`scripts/check-dockerfile-versions.sh`, and the `docker/metadata-action`
label rules of [0014](0014-oci-image-labels-and-sbom-coverage-namespace.md)
and the per-arch mechanics of
[0016](0016-multi-arch-ci-native-runners.md). template-axum and
template-react carried copies of the same. `template-docker` v1.0.0 is the
shared image layer on top of base (its own ADR 0001): a template-owned
`Makefile` tuned by an instance's optional `image.mk`, the smoke, scan,
skopeo and runner-setup scripts, and a skeleton `Dockerfile`. The old
`publish` also pushed a rebuild of the image rather than the tar that
`sbom` described and that had been tested; template-docker pushes the
tested tar with skopeo.

## Decision

We will sync from `template-docker` (`.github/template-sync-state.json`:
`craftainer/template-docker`, `v1.0.0`; it already carries base v1.7.0) and
stay a template ourselves, editing `.github/template-sync-manifest.yml`
directly (it started as base's, then docker's, and adds fastapi's own
paths, so every tracked path is classified).

**Moved or deleted.** The repo's `Makefile` recipes, `scripts/runner-setup.sh`
and `scripts/check-dockerfile-versions.sh` are replaced byte for byte by
docker's. New from docker (replace tier): `scripts/image-smoke.sh`,
`scripts/image-scan.sh`, `scripts/skopeo.sh`, `scripts/runner-setup.d/README.md`,
plus the newer `release.yml` and workflow docs, Renovate managers and the
`hadolint`, `check-dockerfile-versions` and manual-stage `image-scan` hooks.
`.dockerignore` becomes merge tier (docker's baseline plus fastapi's
entries); `image.mk` is merge tier; `Dockerfile` stays replace tier for
fastapi's own instances; `scripts/runner-setup.d/10-remove-pip.sh` is replace
tier (listed one by one, no ignore glob), and an instance's own runner hooks go in
its `template-sync-manifest.local.yml`.

**`Dockerfile`** follows docker's layout: `syntax=dockerfile:1.28`; the runner
stage runs `runner-setup.sh` (user, group-0 home, `/app/data`) before the
app is copied; `runner.sh`, `alembic.ini` and `alembic/` are then copied
read-only (`--chown=<uid>:0`, with numeric `--chmod` 0440 for `alembic.ini` and
0550 for `alembic/`: the symbolic `u=rX,g=rX,o=` form failed on the CI runner's
stock Docker, where alembic could list `alembic/versions` but not open the files). The old script's recursive
`chgrp`/`chmod` of `/app` is gone, and so is `/app` being group-writable:
the app does not write there (bytecode writing is off, migrations only read
`alembic/`). The venv keeps its `--chown`/`--chmod=750` copy.

**`image.mk`** sets only what differs from docker's defaults:

| Variable | Value | Why |
| --- | --- | --- |
| `BUILD_ARGS` | `--build-arg APP_VERSION=$(RELEASE_VERSION) --build-arg APP_COMMIT=$(shell git rev-parse HEAD)` | the app exposes both as env vars |
| `OCI_DESCRIPTION` | one-line service description | docker has no offline source for the repository description |
| `COVERAGE_REPORT` | `coverage.xml` | `unit-tests` writes it to `dist/` and the release attaches it, so the label is kept |
| `IMAGE_SMOKE_PORT` / `_PATH` / `_TIMEOUT` | `8000` / `/health/live` / `60` | same as docker's defaults, stated for clarity |
| `IMAGE_SMOKE_RUN_ARGS` | `-e MODE=mock -e ALLOW_MOCK_MODE=1` | per-arch smoke on a bare runner: mock mode needs no infrastructure |
| `release-check: unit-tests stack-smoke` | see below | steps beyond docker's image smoke |

`ARCHES` (`amd64 arm64`), `OCI_LICENSE` (`GPL-3.0-or-later`), `DOCKERFILE`
and `TARGET` keep docker's defaults.

**NFR-0010 (release smoke) amended**, per the plan's 2026-10-21 decision. The
real-services smoke is `stack-smoke` in `image.mk`, a `release-check`
prerequisite: it runs in the devcontainer, where the stack is up, before
`finalize`, the git tag and the GitHub release. It runs the image through
docker's `image-smoke.sh` on `/health/ready` with `MODE=dev`, the stack's
plain-text hostnames pinned with `--add-host` (the devcontainer's Docker
daemon cannot join the compose network), the stack's env files and a 150s
timeout. Each per-arch image gets a mock-mode `/health/live` smoke in
`release-assets` before `publish`. The root `compose.yml` stays: `smoke.yml`
and `perf.yml` use it, and `smoke.yml` still checks the image in
`MODE=production` against a TLS stack on every push and pull request (which
`stack-smoke` cannot, as the devcontainer stack serves no TLS). It no longer
backs the release gate, so NFR-0010's "Implemented in" line points at
`stack-smoke`.

**Behaviour changes.** `publish` pushes the tested tar, not a rebuild. The
`licenses` label is `GPL-3.0-or-later` (was the deprecated id `GPL-3.0`). The
`description` label is added, and `url` is kept. `finalize` builds the
manifest list from the digests `publish` recorded rather than from tags.
`check-dockerfile-versions` now compares `DEBIAN_VERSION` and
`PYTHON_VERSION` only because the hook lists them; `UV_VERSION` is not listed
(the two Dockerfiles pin different uv versions, which Renovate keeps
separately). The runner image's `/app` is no longer group-writable.

0014 and 0016 get supersede notes for the parts docker now owns (label and
SBOM production; the multi-arch release mechanics).

## Consequences

- Future template-docker releases arrive through template sync; the
  `Makefile` and image scripts are no longer edited here. Per-repo values go
  in `image.mk`.
- The registry image is the tested artifact; `release-check` now boots the
  image twice more (mock mode and against the stack), adding about a minute.
- The stack smoke runs in `MODE=dev`, so production-only guardrails are
  covered by `smoke.yml` alone; a regression there will not block a release.
- A per-arch `<version>-<arch>` registry tag of an image that later fails the
  real-services smoke can exist, but no version tag, manifest list or
  release can.
- Publishing needs `skopeo`, which docker's script runs from a pinned image.
