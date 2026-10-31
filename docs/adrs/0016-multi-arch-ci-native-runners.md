# 0016. Multi-arch CI via native arm64 runners, no QEMU except where unavoidable

## Status

Accepted. The `release.yml` build/manifest mechanics are superseded by
[0024](0024-re-parent-onto-template-docker.md): template-docker now owns
that workflow and the Makefile behind it (the per-arch tag is the tested tar
pushed with skopeo; `finalize` builds the manifest list from the pushed
digests). Native arm64 runners and the `CI_RUNNER_AMD64`/`CI_RUNNER_ARM64`
split stand.

## Context

An arm64 devcontainer build/run was manually verified working
end-to-end on 2026-09-06 (stack images, `uv sync`, ruff/mypy/pytest/e2e,
and the `runner` image all pass natively on aarch64), but nothing in CI
proved this on an ongoing basis — `checks.yml`, `smoke.yml`, and
`release.yml` all only ever ran on `ubuntu-latest` (amd64).

`template-fastapi` is public, which makes GitHub-hosted
`ubuntu-24.04-arm` runners free and billed as ordinary public-repo
Actions minutes rather than metered arm64 pricing — a real,
non-emulated Linux arm64 runner, not QEMU-under-amd64. That changes the
calculus from "emulate arm64 to save runner cost" to "just build arm64
natively," so the design below avoids QEMU everywhere it's avoidable, and
uses it only where a single job genuinely needs to produce output for an
architecture its own runner isn't.

## Decision

`checks.yml`'s `prek` job and `smoke.yml`'s `smoke` job each run as a
`strategy.matrix` with two legs (`amd64` on `ubuntu-24.04`, `arm64` on
`ubuntu-24.04-arm`), both native. Each leg can be overridden
independently via `CI_RUNNER_AMD64`/`CI_RUNNER_ARM64`
repository/organization variables, split out from the pre-existing
`CI_RUNNER` variable (which still governs the workflows/jobs that aren't
matrixed by arch: `perf.yml`, `template-sync.yml`, and `release.yml`'s
non-`build` jobs) — this is a breaking rename for anyone already relying
on `CI_RUNNER` to steer a self-hosted *arm* runner specifically, but
`CI_RUNNER` alone never disambiguated arch in the first place, so no
existing single-arch usage breaks.

`release.yml`'s image build becomes a `build` job matrixed the same way,
each leg building the `runner` stage natively for its own arch (no
`docker/setup-qemu-action`, no multi-platform single-`buildx` call) and
producing its own OCI tarball, SPDX SBOM, and (if `OCI_REGISTRY` is
configured) its own `<version>-<arch>`-tagged registry push. A separate
`manifest` job then runs `docker buildx imagetools create` to combine
the two registry tags into one real multi-arch manifest list at the
plain `<version>` tag — this needs no QEMU either, since
`imagetools create` only manipulates registry manifests, never rebuilds
an image. Both tarballs
(`template-fastapi-<version>-amd64.tar`/`-arm64.tar`) and both
per-arch SBOMs are attached to the GitHub release, rather than one
amd64-only tarball (today's behavior) or one multi-platform OCI-index
tarball (which plain `docker load` can't consume) — arm64 users get an
equally loadable artifact, at the cost of double the release-asset count
and upload time.

`Dockerfile`'s `develop` stage is deliberately left unpinned to a
platform: it already builds for the host's native arch via plain
`docker build`/`docker compose build`, so an arm64 contributor already
gets a native arm64 devcontainer. Forcing it to `linux/amd64` would buy
no verification benefit (CI is what needs to prove cross-arch
correctness) while pushing every arm64 contributor's inner loop through
QEMU emulation.

Every `runs-on` default across all five workflows is pinned to the
concrete `ubuntu-24.04` instead of the floating `ubuntu-latest`, so the
amd64 leg tracks the same Ubuntu release as `ubuntu-24.04-arm` instead
of drifting whenever GitHub rolls `ubuntu-latest` forward.

## Consequences

Easier: arm64 is now a continuously-checked, continuously-shipped
target — a break that only manifests on aarch64 (e.g. a C-extension
dependency missing an arm64 wheel) is caught by `checks.yml`/`smoke.yml`
on every push/PR instead of relying on periodic manual verification.
Native builds are also faster and more reliable than QEMU-emulated
cross-builds would have been for the same coverage.

Harder: `checks.yml`/`smoke.yml` now run twice as many jobs per
push/PR, and `release.yml` gained two extra jobs (`build` is now
matrixed, plus a new `manifest` job) and doubles the release-asset
count. The `CI_RUNNER_AMD64`/`CI_RUNNER_ARM64` split is a breaking
rename for any self-hosted-runner user who set the old `CI_RUNNER`
specifically to steer arm64 work — none are known to exist for this
template today. Should this repo's visibility or the arm64-runner
free tier ever change, the resolved "runner cost" question above would
need revisiting (a private fork pays ordinary metered minutes for
`ubuntu-24.04-arm`, same as any other Actions minute).
