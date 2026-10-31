# FR-0013. Authenticate requests via OIDC bearer tokens

## Status

Implemented

## Description

The system shall require a valid OIDC bearer token (Authorization
Code + PKCE flow) on any route depending on `get_current_claims`,
validating signature, issuer, audience (if configured), and expiry
against the configured provider's discovery document and JWKS —
working with any Authorization-Code+PKCE-capable provider, not only
Keycloak. A missing or invalid token shall be rejected with 401 and a
`WWW-Authenticate: Bearer` header. Interactive API docs shall support
logging in via OAuth2/PKCE using the configured client id.

## Source

Security/compliance; API consumers. See
[0003-auth-strategy-and-federated-backends](../adrs/0003-auth-strategy-and-federated-backends.md).
Implemented in `src/app/oidc.py`, `src/app/main.py`.

## Acceptance criteria

- A request to a protected route with no `Authorization` header
  returns 401.
- A request with a malformed, expired, or wrong-audience/issuer token
  returns 401 with `WWW-Authenticate: Bearer`.
- A request with a valid token succeeds and the route can read its
  claims.
- Swagger UI's "Authorize" flow completes a PKCE login against the
  configured provider.
