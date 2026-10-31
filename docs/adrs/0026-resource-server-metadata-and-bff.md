# 0026. Validate tokens as a resource server and publish its metadata, for a BFF in front

## Status

Accepted. Amends [0003](0003-auth-strategy-and-federated-backends.md): the
validation pattern there is unchanged, but "only `OIDC_ISSUER_URL` differs per
deployment" no longer holds (see Decision), and the caller may now be a
backend-for-frontend rather than the browser.

## Context

The plan in `docs/plans/2026-10-bff-wellknown-analytics-owasp.md` puts a
backend-for-frontend (BFF) between the browser and this API: the BFF holds the
OIDC session, and calls the API with a bearer token whose `aud` contains `api`.
Three things in the way 0003 was implemented stop that from working safely:

- One variable, `OIDC_ISSUER_URL`, was both the `iss` a token must carry and the
  URL discovery is fetched from. Keycloak now runs with a fixed public hostname
  (`KC_HOSTNAME=http://localhost:8080`), so tokens carry that `iss` while this
  backend, inside the compose network, can only reach `http://keycloak:8080`. A
  single value cannot be both.
- The `aud` check ran only when `OIDC_AUDIENCE` was set, and development and mock
  left it unset. RFC 9700 section 2.3 asks resource servers to verify audience;
  a token minted for another client of the same realm should not work here.
- A client that meets a 401 has no standard way to learn which authorization
  server to use. RFC 9728 (OAuth 2.0 Protected Resource Metadata) defines a
  `/.well-known/oauth-protected-resource` document and a `resource_metadata`
  parameter on the `WWW-Authenticate` challenge for that.

The sibling services (the BFF library, template-axum) take the same split, with
`OIDC_ISSUER` and `OIDC_DISCOVERY_URL`.

## Decision

We will:

- Separate the **expected issuer** from the **discovery URL**. `OIDC_ISSUER` is
  the exact `iss` a token must carry (compared as a string, no normalisation);
  `OIDC_DISCOVERY_URL` is the full `.../.well-known/openid-configuration` URL the
  backend fetches. The original `OIDC_ISSUER_URL` keeps working: with neither new
  variable set it is both, as before. Each new variable falls back to the other
  source (`OIDC_ISSUER` to `OIDC_ISSUER_URL`; the discovery URL to the expected
  issuer plus the well-known suffix).
- Verify `aud` always, stricter than merely "on by default" (RFC 9700 section 2.3). `OIDC_AUDIENCE` defaults to `api` and cannot be blank or
  switched off; `exp`, `iss` and `aud` are required claims.
- Serve `GET /.well-known/oauth-protected-resource` (RFC 9728) with `resource`,
  `authorization_servers` (the expected, public issuer), `scopes_supported`,
  `bearer_methods_supported: ["header"]` and `resource_name`, and send
  `WWW-Authenticate: Bearer resource_metadata="<url>"` on every 401, with
  `error="invalid_token"` when a token was presented and rejected. The `resource`
  and the URL come from `PUBLIC_BASE_URL` (required and https in production),
  never from the request's `Host` header, which the client controls.
- Serve `/.well-known/security.txt` (RFC 9116) only when `SECURITY_TXT_CONTACT` is
  set. Settings refuse to start if a contact has no `Expires`, or an `Expires`
  that is naive, past, or more than a year ahead.
- Keep authorization enforcement where 0003 put it: in this backend, per request
  (NFR-0011). The BFF forwards a token; it is not trusted to decide access.

## Consequences

- Backends behind a public hostname different from their in-network one work
  without patching tokens or disabling the `iss` check. An instance running on
  `OIDC_ISSUER_URL` alone behaves as before, except for the audience.
- An instance whose tokens carry no `aud`, or a different one, fails every
  request until its provider adds an audience mapper or `OIDC_AUDIENCE` matches.
  That is the intended trade for RFC 9700; there is deliberately no off switch.
- `PUBLIC_BASE_URL` is a new required setting in production, and the metadata is
  only as right as that value. A reverse proxy needs no `Forwarded` handling for
  it, since the value is not derived from requests.
- Two more public routes exist (three with the existing `api-catalog`); both go
  through the security-headers middleware and carry no secrets.
- `security.txt` is opt-in, so a fresh instance advertises no reporting channel
  until its owner picks one. The suggested contact is the repository's private
  vulnerability-reporting form, `https://github.com/craftainer/<repo>/security/advisories/new`.
- `Expires` has to be renewed at least yearly; a process that outlives it keeps
  serving the old date until restarted.
