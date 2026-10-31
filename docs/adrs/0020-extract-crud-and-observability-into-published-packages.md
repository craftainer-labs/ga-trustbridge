# 0020. Extract `crud` and an observability package into published packages built from a package template

## Status

Accepted

## Context

`src/crud/` and `src/health/` are generic, but they only ship inside
this repo's image (the hatch `packages` list), so other apps cannot reuse
them and they share this repo's release cycle, tests and benchmarks.
`health` alone would be a very small package. Everything a service needs
to be operable (health, dependency checks, logging, request IDs,
OpenTelemetry) lives in or next to it, scattered across `app`.

`ADR-0018` established `template-base` and its instances; there is no
instance for a pure Python library. `crud` also imports five things back
from `app` (settings, `xml_codec`, `web_components`, the rate limiter),
so it is not yet independent.

## Decision

We will publish two packages, both created from a new
`craftainer/template-python-package` (an instance of `template-base`,
and itself a template like `template-fastapi`, so libraries receive
fixes through template sync).

**Names.**

| | crud | observability |
| --- | --- | --- |
| GitHub repo | `craftainer/tool-crud` | `craftainer/tool-observability` |
| PyPI distribution | `craftainer-tool-crud` | `craftainer-tool-observability` |
| Import package | `craftainer_tool_crud` | `craftainer_tool_observability` |

The bare names (`crud`, `health`, `fastapi-crud`, `fastapi-health`) are
taken on PyPI, and a library should not claim a generic top-level import
name another package could shadow. `tool-*` is the org's prefix for
reusable libraries, next to `template-*`.

**Licence.** GPL-3.0 for the packages and the package template for now,
matching this repo.

**Template.** `src/<import_name>/` layout with `py.typed`, hatchling and
`uv`; this repo's quality bar (strict `ruff`, `mypy --strict`, `pytest`
with a 100% coverage gate, `import-linter`, `pip-audit`, all under
`prek`); `template-base`'s release Makefile contract (`release-arches`
`["amd64"]`, `build` via `uv build`, Syft `sbom`, `publish` via
`uv publish --trusted-publishing always`, skipped when the
`PYPI_PUBLISH` variable is unset); and an optional CodSpeed workflow
([`0019`](0019-per-pr-benchmarks-and-fixed-load-acceptance.md)).
Publishing uses PyPI trusted publishing (GitHub OIDC, no stored token);
the first release of each package goes to TestPyPI as a dry run. This
needed one `template-base` change, released as v1.5.0:
`id-token: write` on the per-arch job that runs `make publish`, plus
passing `PYPI_PUBLISH` through to `make publish` and `release-check`
(base's `release.yml` did not pass it), both no-ops for repos that do not
publish. The repository variable `PYPI_PUBLISH` is `testpypi` or `pypi`
and selects the index. On the bare release runner `uv` is absent, so the
package Makefile runs `build`, `sbom` and `publish` in a pinned
`ghcr.io/astral-sh/uv` container image instead.

**Observability package scope.** One package with optional extras:

| Module | Contents | Extra |
| --- | --- | --- |
| `health` | registry, router, `/health/live`, `/health/ready`; opt-in `/health/info` (off by default, it discloses the version) | core (FastAPI only) |
| `health.checks` | generic dependency checks taking an engine, URL or client | `[sqlalchemy]`, `[redis]`, `[s3]`, `[http]` |
| `logs` | JSON formatter, credential redaction, trace-id correlation, OTLP log bridge | core; OTLP in `[otel]` |
| `otel` | tracer and meter providers, `instrument_app()`, `instrument_engine()`, pool gauges ([`0021`](0021-opentelemetry-traces-and-metrics-pushed-over-otlp.md)) | `[otel]` |
| `middleware` | pure-ASGI request-ID middleware (validated, echoed, attached to logs and the active span) | core |
| `profiling` | opt-in pyinstrument hook, off unless enabled, refused under `MODE=production` | none; `pyinstrument` is a dev dependency of the app, imported lazily |

Out of scope: RFC 9457 problem details (API errors, not observability), a
Prometheus `/metrics` endpoint, and a shutdown "readiness drain"
(Kubernetes already removes terminating pods from Service endpoints, and
a `preStop` sleep covers propagation).

**Decoupling `crud` from `app`.** `crud` stops reading settings: `app`
builds the engine and session factory and passes them in with a small
`crud`-owned config object. `xml_codec` and `web_components` move into
`crud`. The rate limiter stays in `app`; router factories take an
optional rate-limit decorator, so `crud` does not depend on `slowapi`.
`aiomqtt` and `defusedxml` become the `[mqtt]` and `[xml]` extras.
Migrations stay app-owned: `crud`'s revision table registers on the
shared `Base.metadata`, and a `crud` release that changes its models
needs a new app-side migration (`NFR-0019`). An `import-linter`
independence contract forbids either package importing `app`, and
observability importing `crud`.

**Moving and consuming.** History is preserved with `git filter-repo`
limited to each package's paths; each package was released as `0.1.0`
(a `0.1.0rc1` release candidate on TestPyPI first, then `0.1.0` on PyPI)
and is versioned as SemVer, staying `0.x` until the public API settles.
`0.1.0` shipped without the `py.typed` marker (the extraction deleted the
template's placeholder package, which carried it), so mypy treated the
packages as untyped. `0.1.1` added `py.typed` with a guard unit test in
each package, and the package template's `release-check.sh` now fails a
release whose wheel lacks `<import>/py.typed`. This repo pins exact versions
(`craftainer-tool-crud[mqtt,xml]==0.1.1`,
`craftainer-tool-observability[otel,sqlalchemy,redis,s3,http]==0.1.1`), Renovate
bumps them, and the in-tree copies and their moved unit tests are
deleted. The e2e suite and `tests/integration/test_health_checks.py`
stay here as the packages' integration tests against real backing
services. Because a package's models now arrive as a lockfile bump, the
`migration-compat.yml` workflow triggers on `uv.lock` rather than on the
deleted in-tree model files. Editing a package alongside the app uses a local-only `uv`
path override that is never committed. Because `src/` is synced to this
template's instances, they receive the deletion and new pins in their
next sync PR.

## Consequences

Easier: other apps reuse `crud` and the operability stack; each package
has its own release cycle, tests and benchmarks; the independence
contracts make a back-import impossible once code has moved.

Harder: a change spanning a package and the app becomes two PRs and a
release; the app depends on PyPI availability and exact pins; a `crud`
model change needs a coordinated app-side migration. Three new repos and
a template carry maintenance (each repo has one "Protect main" ruleset
applied by hand, because org rulesets are not enforced on the Free plan;
`TEMPLATE_SYNC_TOKEN` needs the repos to stay public) and PyPI trusted-publisher setup. Template
instances take a one-time dependency swap. The packages are `0.x`, so
their public API may still change, and the GPL-3.0 licence applies to
every consumer; revisiting it is a separate decision.
