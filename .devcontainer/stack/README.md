# stack/

One subdirectory per supporting service (database, object storage,
cache, identity provider, e2e test runner, ...). Each contains:

- `compose.yml` — the Compose fragment for that service.
- `README.md` — what it is, connection details, and how to remove it.
- `<service>.env` — that service's local-dev-only default credentials;
  see "Configuration" below — only present when the service actually has
  credentials to hold (redis and selenium don't).

Every fragment here is listed in `../compose.yml`'s own `include:` list,
so all of them start together. Each fragment's relative bind-mount or
`env_file:` path — like keycloak's `realm-export.json` or
`keycloak.env` — resolves relative to *that fragment's own directory*,
the same as if it were the only Compose file in play; see each
fragment's own header comment.

## Devcontainer stack pattern

`../compose.yml` defines the app service and pulls every supporting
service in via this `include:` list — not the `dockerComposeFile` array
in `devcontainer.json`, which only ever names `../compose.yml` itself. A
new service follows this same `compose.yml` + `README.md` pattern and
gets added to `../compose.yml`'s `include:` list.

No fragment here (including the app service's own `../compose.yml`)
declares a `ports:` mapping or a `networks:` block:

- **No `networks:`** — every service already reaches every other one by
  its service name on the default network Compose generates for this
  project; a named network here would only add a place for that to drift.
- **No `ports:`** — these are backend services meant to be reached only
  from other containers. If a service genuinely needs a host-browser-
  reachable port (Keycloak's login UI is the one example here), that
  port goes in `forwardPorts`/`portsAttributes` in `../devcontainer.json`
  instead — the Dev Containers spec forwards a container's port directly
  from its network namespace, so this doesn't require publishing it via
  Compose at all. This keeps "what's reachable from the host" defined in
  exactly one place instead of scattered `ports:` blocks.

Every stack fragment's service also defines a `healthcheck:`, and
`../compose.instance.yml`'s `myapp` service lists a matching `depends_on:
<service>: condition: service_healthy` entry for it. Compose won't start
`myapp` until every dependency reports healthy, so `docker compose up`
(and therefore `devcontainers/ci`'s `runCmd`, and a fresh
`postCreateCommand`) never runs a check or a test against a stack
service that's still starting — e.g. Keycloak, a JVM app doing
`start-dev --import-realm`, takes far longer to accept requests than
Postgres/Redis/RustFS do. Pick each healthcheck from what the image
actually ships — verify with `docker run`/`docker compose up` against
the real pinned image rather than assuming a tool (`curl`, `wget`) is
present; `keycloak/compose.yml`'s healthcheck uses bash's `/dev/tcp`
instead of `curl` because the official Keycloak image ships neither
`curl` nor `wget`.

## Configuration

Every stack service's own local-dev-only default credentials (Postgres,
RustFS, Keycloak's admin/test users, ...) live in a `<service>.env` file
inside that service's own subdirectory here — the single source of
truth for that service's values, tracked in git since these are
dev-only defaults, not real secrets (each service's `README.md` says
so). That fragment's own `compose.yml` loads its file via `env_file:`
directly into the service's container. Where `myapp` needs those same
values (`../compose.instance.yml`), it lists the same per-service files under its
own `env_file:` — never re-pins the values a second time; see
`../../src/app/README.md`'s "Configuration" section for how `app.config`
assembles them into the settings the app itself reads.

## Do

- Give a new service its own subdirectory following this same
  `compose.yml` + `README.md` pattern, plus a `<service>.env` if it has
  credentials or other config values to hold.
- Give the service a `healthcheck:`, and add a matching `depends_on:
  <service>: condition: service_healthy` entry to `myapp` in
  `../compose.instance.yml` — see "Devcontainer stack pattern" above for why, and
  verify the healthcheck command against the real pinned image before
  trusting it (don't assume `curl`/`wget` are present).
- Let it join the default network compose generates for this project —
  don't declare a `networks:` block; every service reaches every other
  one by its service name already.
- If host-browser access to the service is genuinely needed (as with
  keycloak's login UI), add its port to `forwardPorts`/`portsAttributes`
  in `../devcontainer.json` — never to a `ports:` mapping here.
- Pin an image tag to the app version only (`postgres:18.6-alpine`),
  not the OS sub-patch a multi-part tag like `postgres:18.6-alpine3.24`
  adds on top — Renovate still bumps the app version fine
  either way, and the extra digits just add unrelated diffs. A tag
  segment that names a genuinely different build variant (`-glibc` vs.
  the default, `-noble` vs. another base OS) isn't this kind of
  precision and stays.

## Keeping the architecture doc current

Adding or removing a stack service, or changing how one connects
(`depends_on`/healthcheck, a newly forwarded port) makes
`../../docs/architecture.md`'s diagram stale — update it in the same
change.

## Don't

- Add a `ports:` entry to any fragment here. These are backend services
  reached only from other containers on the docker network; publishing a
  host port defeats that isolation. See the root README's "Don't".
- Reuse another service's volume or hostname.
- Put real credentials in a fragment here — these are local-dev-only
  defaults; production configuration is a deployment concern, not this
  template's.
