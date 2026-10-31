# scripts/

Setup logic for each `.devcontainer/Dockerfile` / `Dockerfile` RUN step —
every RUN calls one of these (or loops over a hook directory below), never
a binary directly:

- `develop.sh` — `../.devcontainer/Dockerfile`'s `develop`-stage setup: installs `prek`,
  the Claude Code CLI, `snip` and the GitHub CLI (`gh`), the only tooling this template requires
  regardless of instance language. An instance adds its own language
  runtime/tooling install on top, either by extending this script or
  copying its shape into a second script. Prefer the hook below over
  either. Every download goes through its `fetch` helper (bounded curl
  retries with backoff; GitHub URLs authenticated when the optional
  `github_token` BuildKit secret is present) and the installers
  that download for themselves run through `retry` (the installer script
  itself is fetched once, by `fetch`, so the two layers of retries do not
  multiply; a failed download names the installer and URL before the script
  aborts, an empty one is rejected, and `fetch` itself retries DNS and
  connect failures (curl exit 6/7) and truncated transfers (18/52/55/56) up
  to 4 times; timeouts are left to curl's own `--retry`), so CI's shared-IP
  HTTP 429s don't fail the image build. Local builds need no token.
- `post-create.sh` — the devcontainer's `postCreateCommand`: git
  `safe.directory`, `prek install`, then every `post-create.d/*.sh`.
- `develop.d/`, `post-create.d/`, `runner-setup.d/` — instance hook
  directories; see `docs/TEMPLATE.md`'s "Instance extension points".
  template-fastapi's own hooks:
  - `develop.d/NN-*.sh` — FastAPI-specific extra develop tooling (apt
    clients, pyright, rustfs CLI, `kcadm`), run after `develop.sh`; each
    pins its own version with a `# renovate:` comment.
  - `post-create.d/NN-*.sh` — devcontainer post-create steps (`uv sync`,
    migrations).
- `builder.sh` — `../Dockerfile`'s `builder` stage tooling setup (apt
  packages, `uv`).
- `builder-sync-deps.sh` / `builder-sync-app.sh` — the `builder` stage's
  two `uv sync` steps, split so a source-only change doesn't invalidate
  the dependency-install layer.
- `runner.sh` — the `runner` stage's entrypoint (starts the app, as PID 1
  via `exec`). `APP_SERVER` picks the server: `uvicorn` (default) or
  `granian`; any other value exits 64 with a message.
  - uvicorn runs with `--loop uvloop --http httptools` (a missing wheel
    fails loudly). Every other tunable is an unset-by-default
    `UVICORN_*` env var that uvicorn's CLI reads natively, e.g.
    `UVICORN_ACCESS_LOG=false`, `UVICORN_PROXY_HEADERS`,
    `UVICORN_BACKLOG`, `UVICORN_TIMEOUT_KEEP_ALIVE`,
    `UVICORN_LIMIT_CONCURRENCY`, `UVICORN_WORKERS`. Note that
    `--limit-concurrency` makes uvicorn answer with its own plain-text 503
    before the app runs, so that response is not RFC 9457 problem details.
  - granian runs `--interface asgi` on the same host/port; tune it with
    `GRANIAN_*` env vars (`GRANIAN_WORKERS`, `GRANIAN_BACKLOG`,
    `GRANIAN_LOOP`, `GRANIAN_LOG_ACCESS_ENABLED`, ...).
  - `GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS` (unset by default) maps to
    uvicorn's `--timeout-graceful-shutdown`. Granian has no such option;
    the closest is `GRANIAN_WORKERS_KILL_TIMEOUT`, which runner.sh sets
    to the same value.
  - `APP_VERSION` / `APP_COMMIT` are baked in by the `Dockerfile`
    `ARG`s (set by `make build`).
- `lint-migrations.sh` — the `lint-migrations` prek hook and
  `../.github/workflows/migration-compat.yml`'s Squawk step (NFR-0027):
  renders each given revision under `../alembic/versions/` to SQL with
  `alembic upgrade <down_revision>:<revision> --sql` (offline, no
  database) and lints it with `squawk`, which flags locking and rewriting
  operations. `--changed-only` first narrows the files to those that
  differ from the merge base with `origin/main`/`main` (applied
  migrations are frozen, NFR-0019); without it every given file is
  linted. `SQUAWK_ARGS` passes extra arguments to `squawk`.
- `runner-setup.sh` — optional; the top-level `Dockerfile`'s `runner` stage setup:
  group-0 runtime user for arbitrary-UID platforms (run before copying
  app code, which is then copied read-only), then every
  `runner-setup.d/*.sh`. An instance whose runner image doesn't call it
  (an nginx runner, say) ignores it and `runner-setup.d/`.
- `image-scan.sh` — the manual-stage `image-scan` hook: Trivy over the
  tar for the current `RELEASE_VERSION`/arch in `dist/`.
- `test-image-scripts.sh` — the `image-scripts-test` hook: offline tests of
  the Makefile's repo derivation and these scripts.
- `image-smoke.sh` — `make image-smoke` (a `release-check` prerequisite):
  converts the arch's image in the release OCI tar from `dist/` to a
  docker-archive with the pinned skopeo (`docker load` can't read a buildx
  OCI tar on the classic image store), loads and runs it, and polls
  `IMAGE_SMOKE_PATH` until 2xx. Fails when there is no tar for the arch under
  test. Needs `SKOPEO_IMAGE`, which the Makefile exports; usage in the
  script's header.
- `skopeo.sh` — runs the pinned skopeo image as the caller's user with
  `dist/` mounted at `/dist`; used by `make publish` and `image-smoke.sh`.
- `check-dockerfile-versions.sh ARG...` — fails when a named `ARG` default
  differs between `.devcontainer/Dockerfile` and the top-level `Dockerfile`.

## Do

- Pass a token to the build as a BuildKit secret (`github_token`), never
  a `--build-arg`/`ARG`: an ARG is baked into the image history.
- Keep each script runnable and idempotent on its own (`bash
  scripts/develop.sh <args>`) so you can debug a stage without a full
  build.
- Take a version as a script argument (see `develop.sh`) rather than
  hardcoding it, when the Dockerfile already defines it as an `ARG` —
  keeps that `ARG` the single source of truth.
- Clean up apt lists / caches at the end of a script that installs
  packages, to keep the resulting layer small.
- Keep this script's own tools pinned to an exact version (a `# renovate:`
  ARG in `../.devcontainer/Dockerfile`) and checksum-verified where the upstream project
  publishes checksums, matching the pattern each install step here already
  follows.

## Don't

- Invoke a binary directly from a Dockerfile `RUN` — add or extend a
  script here instead.
- Install tooling a different stage needs — `runner.sh` in particular
  should stay a plain entrypoint, not a setup script.
- Add a language runtime or stack-specific tool to `develop.sh` — that
  belongs in the instance's own layer (`develop.d/`), not the base
  template's.
