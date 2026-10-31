# 0014. Label released images with standard OCI metadata and custom SBOM/coverage pointers

## Status

Accepted. How the labels and SBOM are produced is superseded by
[0024](0024-re-parent-onto-template-docker.md): the Makefile of
template-docker now sets the label set (no `docker/metadata-action`, no
`anchore/sbom-action`; Syft runs from `make sbom`, `licenses` is
`GPL-3.0-or-later`, the coverage label is set through `image.mk`'s
`COVERAGE_REPORT`). The label namespace, the pointers at release assets and
the Python-specific coverage limitation stand.

Note (2026-10-26, LIC1): the `licenses` label is now `GPL-3.0-only`; the `GPL-3.0-or-later` above records the earlier state.

## Context

`release.yml` published an OCI image (tarball, plus an optional registry
push) with no labels at all — nothing on the image itself pointed back
at what commit it was built from, what license applied, or where to
find a bill of materials or a test-coverage report for the exact
artifact. The template needed a way to make the image self-describing
that would hold up regardless of what language/stack a given instance
of the template is written in, since `release.yml` itself is meant to
stay generic.

Two sub-decisions fell out of that:

- **SBOM format**: the tool needed to introspect the built image's
  layers directly (language-agnostic) rather than a language-specific
  dependency manifest, since the app source and its runtime image
  contents can diverge (e.g. multi-stage builds, OS packages).
- **Custom label namespace**: `org.opencontainers.image.*` is reserved
  for the OCI spec's own keys, so pointers to the SBOM and coverage
  report needed their own namespace. That namespace also needed to
  survive being copy-pasted across every repository instantiated from
  this template.

## Decision

We will use `docker/metadata-action` to derive the standard
`org.opencontainers.image.title` / `.description` / `.url` / `.source`
/ `.revision` / `.created` / `.version` labels from git/GitHub context,
applied identically to both the OCI tarball and the registry push.

We will generate the SBOM with `anchore/sbom-action` (Syft) in
SPDX-JSON format, run against the *built* image rather than the
source tree, so the step doesn't need to change if the template's
language/stack changes.

We will point at the SBOM and coverage report via two custom labels,
namespaced as `io.github.<repository_owner>.sbom` and
`io.github.<repository_owner>.coverage-report`, with
`repository_owner` substituted from `github.repository_owner` at run
time — not the repo name too, and not hardcoded. This keeps the
namespace to one well-known prefix per org, stable across every
repo/template instance that org owns, rather than a different prefix
per repo that consumers would have to look up each time.

Both labels point at GitHub release assets (not anything embedded in
the image), computed from the release tag before the image build step
since GitHub's release-asset download URL is deterministic. Only
`release.yml`'s published image gets these labels — `checks.yml` and
`smoke.yml` build images solely to test them in CI and never publish
or distribute them.

Coverage generation (`uv run pytest --cov-report=xml`) is *not*
language-agnostic the way the label mechanism and SBOM step are — it's
tied to this repo's current Python/`coverage.py` toolchain and would
need rewriting if the stack ever changed language. The Cobertura-style
XML output format itself is a cross-language convention; only the
generation step is Python-specific. This is accepted as a known, scoped
limitation rather than building an abstraction layer for a hypothetical
future stack.

## Consequences

- Anyone who pulls the published image can find its SBOM and coverage
  report without knowing anything about this repository beyond the
  image's own labels — `docker inspect` is enough.
- Every repo instantiated from this template shares one label
  namespace per owning org (`io.github.<owner>.*`), so a consumer that
  already knows the convention for one repo knows it for all of them.
- `release.yml` now runs the test suite itself (previously it trusted
  `checks.yml` to have gated the commit already), which adds one more
  `devcontainers/ci` invocation and compose-stack teardown to the
  release job's runtime.
- If the template's stack ever stops being Python, the SBOM step and
  label mechanism carry over unchanged, but the coverage-generation
  step needs a rewrite for the new stack's tooling.
