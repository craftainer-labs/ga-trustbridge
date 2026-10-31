# keycloak

Keycloak, the OIDC identity provider the API authenticates against (see
`src/app/oidc.py`, which validates against any PKCE-capable OIDC provider,
not just Keycloak). Starts in dev mode and
auto-imports `realm-export.json`, so a working realm, client, and test user
exist immediately — no manual admin-console setup required.

- Compose file: `compose.yml`
- Image: `quay.io/keycloak/keycloak:26.7.4`
- Host (from other containers): `keycloak`
- Port (container-internal): `8080`
- Realm: `template-fastapi`
- Clients (all in `realm-export.json`):
  - `api` — public, auth-code + PKCE (S256) only, no password grant. Used
    by Swagger UI: redirect URI `http://localhost:8000/docs/oauth2-redirect`,
    web origin `http://localhost:8000` (exact, no wildcards). Carries the
    client roles the backend checks (`resource_access.api.roles`).
  - `bff` — confidential (`client-secret`), auth-code + PKCE for a
    backend-for-frontend; redirect URIs `http://localhost:5173/bff/callback`
    and `http://localhost:8081/bff/callback`, post-logout URIs the two
    origins (`##`-separated in the attribute), back-channel logout to
    `http://host.docker.internal:8787/bff/backchannel-logout`, refresh
    tokens on, token exchange off. Its secret is **dev-only**: `BFF_CLIENT_SECRET`
    in `keycloak.env`, substituted into the import as `${BFF_CLIENT_SECRET}`.
  - `test-cli` — **dev-only**, public, password grant only (no browser
    flow). Tests and perf scripts mint tokens with it; never create an
    equivalent outside local dev.
  All three add `api` to access tokens' `aud` (audience mapper). Roles reach
  tokens through the default `roles` scope (full scope allowed), so every
  client yields the same `resource_access.api.roles`.
- Realm: refresh tokens rotate (`revokeRefreshToken`, `refreshTokenMaxReuse: 0`);
  SSO session idle 1800 s, max 36000 s.
- Hostname: `KC_HOSTNAME=http://localhost:8080` plus
  `KC_HOSTNAME_BACKCHANNEL_DYNAMIC=true` (see `compose.yml`), so `iss` is
  always `http://localhost:8080/realms/template-fastapi`, even for tokens
  minted via `http://keycloak:8080`, while discovery and back-channel calls
  still work on the internal name. `extra_hosts` maps
  `host.docker.internal` for back-channel logout calls to a host-side BFF.
- Admin console user / password: `admin` / `admin`
- Test users / password (same as username): `viewer`, `editor`, `security`,
  `maintainer`, `detective` — all with the same `offline_access` /
  `uma_authorization` realm roles, plus one `api` client role each
  matching their username (`clientRoles` in `realm-export.json`) —
  what each role grants is up to the app's own routes; see
  `src/app/README.md`'s "RBAC" section for `src/app/oidc.py`'s
  `require_roles` and how routes enforce it.
- Issuer (`iss` claim): `http://localhost:8080/realms/template-fastapi`; discovery/token/JWKS from other containers: `http://keycloak:8080/realms/template-fastapi`
- Admin credentials and the `OIDC_*` URLs: defined in `keycloak.env`, next to this file

Unlike the other stack services, Keycloak's port is listed in
`forwardPorts` in `.devcontainer/devcontainer.json` so you can reach the
login and admin UI from your host browser at `http://localhost:8080`. Since
Keycloak runs in a sibling container rather than the primary `myapp` one, that
entry must use the `"keycloak:8080"` (`serviceName:port`) form, not a bare
`8080` — a bare port number only forwards from the primary container. That
still isn't a compose `ports:` mapping — see the root README's "Don't" for
why the two are different.

From inside the devcontainer, `kcadm` (installed by `scripts/develop.sh`) is
a thin curl wrapper around the Admin REST API — a lighter alternative to the
official `kcadm.sh`, which ships only inside Keycloak's full server
distribution and needs a JVM neither this stage nor the app otherwise
requires. It logs in as `KEYCLOAK_ADMIN`/`KEYCLOAK_ADMIN_PASSWORD` (from
`keycloak.env`, already in the devcontainer's environment) and takes an
HTTP method, an Admin REST API path, and an optional JSON body:

```bash
kcadm GET /admin/realms/template-fastapi/users
kcadm PUT /admin/realms/template-fastapi/users/<id> '{"firstName": "Dev"}'
```

## Do

- Use the test users or `admin` for local development only.
- Edit `realm-export.json` and recreate the container (`docker compose up -d
  --force-recreate keycloak`, or rebuild the devcontainer) to change the
  realm, add clients, or add users — it's re-imported on every start.

## Don't

- Add a `ports:` entry to this file; use `forwardPorts` in
  `devcontainer.json` instead (see above).
- Reuse these defaults, or `realm-export.json` as written, anywhere but
  local dev.

## Removing this service

Delete this directory, remove its compose file entry from
`.devcontainer/compose.yml`'s `include:` list, and remove
`"keycloak:8080"` from `forwardPorts`/`portsAttributes` in
`devcontainer.json` too.
