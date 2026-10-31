# NFR-0010. Gate releases on a smoke test against real dependencies

## Attribute

Operations / release engineering.

## Description

Before a release is considered verified, the built `runner` image shall
be run against real Postgres, Redis, S3, Keycloak and MQTT, with
`/health/ready` polled (up to 150s) until it reports healthy. A release
whose image never reports healthy shall not proceed to `finalize`, the git
tag or the GitHub release. Each per-arch image additionally passes a
mock-mode liveness smoke (`/health/live`) before it is pushed.

## Source

Operators/SRE; release engineering. Implemented as `image.mk`'s
`stack-smoke` target (a `release-check` prerequisite, run inside the
devcontainer where the stack is up), invoked from
`.github/workflows/release.yml`; the per-arch liveness smoke is
`make release-assets`' `image-smoke` step. The root `compose.yml` and
`.github/workflows/smoke.yml` remain a separate, production-mode check of
the same image on every push and pull request (TLS and credential
guardrails), but no longer gate the release. See
`docs/adrs/0024-re-parent-onto-template-docker.md`.

## Verification

CI: `release.yml`'s `release-check` step fails the release workflow if
`/health/ready` doesn't report healthy within the timeout, before
`make finalize` and `gh release create` run. A per-arch image that fails
its mock-mode `/health/live` smoke fails its `build` job before
`make publish`. The weakening against a gate in front of every push is
that a per-arch `-<arch>` tag of an image that later fails the
real-services smoke can exist in the registry, but no version tag,
manifest list or release can.
