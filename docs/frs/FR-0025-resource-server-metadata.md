# FR-0025. Publish OAuth protected-resource metadata and an optional security.txt

## Status

Implemented

## Description

The system shall accept bearer tokens whose `iss` equals the configured expected
issuer even when the provider is reached at a different URL, shall always verify
the token's audience, and shall publish how a client obtains a valid token: an
RFC 9728 protected-resource metadata document at
`/.well-known/oauth-protected-resource`, referenced from the `WWW-Authenticate`
challenge of every 401. When a security contact is configured it shall also
publish an RFC 9116 `/.well-known/security.txt`. All absolute URLs shall come
from configuration, not from the request.

## Source

Security/compliance; API consumers (including the BFF in front of this API). See
[0026-resource-server-metadata-and-bff](../adrs/0026-resource-server-metadata-and-bff.md),
which amends [0003](../adrs/0003-auth-strategy-and-federated-backends.md).
Extends [FR-0013](FR-0013-oidc-bearer-auth.md). Implemented in `src/app/oidc.py`,
`src/app/config.py`, `src/app/well_known.py`, `src/app/main.py`.

## Acceptance criteria

- With `OIDC_ISSUER` and `OIDC_DISCOVERY_URL` set to different hosts, a token
  whose `iss` equals `OIDC_ISSUER` is accepted and discovery is fetched from
  `OIDC_DISCOVERY_URL`; a token with any other `iss` is rejected with 401.
- With only `OIDC_ISSUER_URL` set, issuer and discovery behave as before.
- A token with a missing or non-matching `aud` (default expected value `api`; a
  list containing it is fine), or without `exp`, is rejected with 401.
- `GET /.well-known/oauth-protected-resource` needs no authentication and returns
  `resource`, `authorization_servers` (the expected issuer), `scopes_supported`,
  `bearer_methods_supported: ["header"]` and `resource_name`, unaffected by the
  request's `Host` header.
- A 401 for a missing token carries
  `WWW-Authenticate: Bearer resource_metadata="<absolute URL>"`; a rejected token
  adds `error="invalid_token"`.
- With `MODE=production`, startup fails unless `PUBLIC_BASE_URL` is an https
  origin and the issuer and discovery URL are https.
- `/.well-known/security.txt` returns 404 unless `SECURITY_TXT_CONTACT` is set;
  when set it returns `text/plain` with `Contact` and `Expires` (UTC) plus any of
  `Policy`, `Preferred-Languages`, `Canonical` that are configured. Startup fails
  if a contact is set without an `Expires` that is timezone-aware, in the future
  and at most one year ahead.
- Both endpoints carry the baseline security headers.
