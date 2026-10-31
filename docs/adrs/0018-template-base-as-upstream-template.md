# 0018. Extract a generic template-base and make template-fastapi its instance

## Status

Accepted

## Context

`template-fastapi` bundled two things that couldn't be separated: a
generic scaffold (devcontainer, CI/release/template-sync machinery,
`.claude`/`.vscode` config, docs/ADR/plan structure) and a full
FastAPI/Postgres/Redis/S3/Keycloak application on top of it. Other repos
want only the scaffold: a library in another language, an infra-only
repo (OpenTofu, Ansible, ...) with no app runtime, or a port of this
template to Rust, Go or Node.js.

`template-sync.yml` already syncs an instance from a template's tagged
releases. Its `apply` step only touches paths matched by the manifest of
the template being synced *from*, so a repo can be an instance of a
template whose manifest covers only part of the repo. Any extraction had
to keep working for repos already created from `template-fastapi`; their
state files point at `craftainer/template-fastapi`.

## Decision

We extract the scaffold into
[`craftainer/template-base`](https://github.com/craftainer/template-base)
and make `template-fastapi` an ordinary instance of it, using the
existing sync rather than new tooling. Generic changes reach a
`template-fastapi` instance in two reviewed PRs: base → template-fastapi,
then template-fastapi → instance. Downstream instances keep their state
files unchanged.

**Extension points instead of `merge`.** A file template-base owns stays
`replace` tier and exposes a hook that the instance fills with files it
owns, so an instance never edits a base-owned file:

| Hook | Base side (template-owned) | Instance side (instance-owned) |
|---|---|---|
| Extra devcontainer tooling | `Dockerfile` `develop` stage runs every `scripts/develop.d/*.sh` after `develop.sh` | `scripts/develop.d/NN-name.sh` |
| Extra post-create steps | `scripts/post-create.sh` runs every `scripts/post-create.d/*.sh` | `scripts/post-create.d/NN-name.sh` |
| Extra compose services / dev-service settings | `devcontainer.json` lists `compose.yml` and `compose.instance.yml` | `.devcontainer/compose.instance.yml` (`ignore` tier) |
| Runtime image | nothing (base `Dockerfile` is `develop`-only) | `app.Dockerfile` + `app.Dockerfile.dockerignore` |

Only files with no include mechanism of their own are `merge` tier: JSON
configs, `.pre-commit-config.yaml` (prek has no include) and docs an
instance appends to.

**`.github/template-sync-manifest.yml` is `merge`, not `replace`.** As
`replace`, the first sync from base would overwrite `template-fastapi`'s
own manifest and break every instance downstream of it. An ordinary
instance never edits the manifest; it lists its own files in the
instance-owned `.github/template-sync-manifest.local.yml` (`ignore` tier
only, read by the completeness check, never by sync). So the merge stays
clean for ordinary instances. A repo that is itself a template, like
`template-fastapi`, edits its manifest directly and resolves base's
additions by hand.

**Release: one generic `release.yml` over a per-instance `Makefile`
contract.** In template-base's manifest, `release.yml` is `replace` tier
and the `Makefile` is `ignore` tier, because it's exactly the part that
differs per artifact type. (`template-fastapi`'s own manifest makes its
`Makefile` `replace` for its instances, which share its artifact type.)

| Target | Runs | Where |
|---|---|---|
| `release-arches` | once, first; prints a JSON array, e.g. `["amd64"]` | host runner |
| `build`, `sbom`, `release-assets`, `publish` | once per arch, in that order | native runner for that arch |
| `release-check` | once, after all arches | inside the devcontainer, stack up |
| `finalize` | once, after `release-check` | host runner |

Per-arch files in `dist/` carry the arch in their name, and `release.yml`
attaches whatever is left in `dist/`. The arch matrix keeps
`template-fastapi`'s native amd64/arm64 release and coverage report (see
[0016](0016-multi-arch-ci-native-runners.md)). `docs/TEMPLATE.md` is the
living specification for both tables, including the environment each
target receives.

## Consequences

- A generic fix lands once in template-base and reaches every family
  member through sync PRs. An app change reaching a `template-fastapi`
  instance still takes one hop, a scaffold change takes two.
- Verified end to end on 2026-09-24/10-17:
  - base → `template-fastapi`;
  - `template-fastapi` → an instance created before the extraction;
  - two repos created straight from base at `v1.2.4`: infra-only with
    OpenTofu and the no-op `Makefile`, and a Go service with a two-arch
    release.
- `template-fastapi` must resolve base's manifest additions by hand on
  every sync that changes base's manifest.
- An instance created before `v0.0.1-alpha.1` can't take its first sync
  unaided. Its `template-sync.yml` pushes with the default
  `GITHUB_TOKEN`, which GitHub refuses for changes under
  `.github/workflows/`, and that first sync always includes some. It
  needs the one-time manual upgrade in `docs/TEMPLATE.md`'s "Template
  sync" section, and a `TEMPLATE_SYNC_TOKEN` secret. In verification, an
  organization-level token only reached the instance once the repo was
  public.
- Renovate's devcontainer manager bumps feature versions but can't
  regenerate `devcontainer-lock.json`, and CI didn't catch the stale
  lockfile: the `devcontainer-lock` check passed in CI on commits where
  it fails locally. Twice an automerged Renovate PR left base's `main`
  failing the check. Base's `renovate.json` therefore turns off
  devcontainer feature updates; features are bumped by hand together
  with the lockfile until Renovate supports it.
- Every instance, in any language, runs `prek run --all-files
  --hook-stage manual` in CI and layers its own hooks on base's
  language-agnostic ones.
