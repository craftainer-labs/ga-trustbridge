# FR-0017. Provide a mock token issuer for RBAC testing without a real IdP

## Status

Implemented

## Description

The system shall, only under `MODE=mock`, mount `POST /mock/token`
to issue a JWT shaped like a real Keycloak token (including
`resource_access.<client>.roles`), and shall skip signature
verification for incoming bearer tokens in that mode, enabling RBAC
testing without a running identity provider.

## Source

Developers, CI. Implemented in `src/app/controllers/mock.py`,
`src/app/oidc.py`.

## Acceptance criteria

- `POST /mock/token` is not mounted under `MODE=dev` or
  `MODE=production`.
- Under `MODE=mock`, a token obtained from `/mock/token` with a given
  role is accepted by role-guarded routes without a real IdP present.
