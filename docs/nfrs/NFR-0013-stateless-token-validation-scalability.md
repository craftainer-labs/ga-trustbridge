# NFR-0013. Keep token validation stateless for regional/federated scaling

## Attribute

Scalability / architecture.

## Description

Bearer-token validation shall remain stateless and local to each
backend instance (JWKS/issuer/audience checks with no shared session
state), so the same `oidc.py` code runs unmodified across multiple
regional deployments; only `OIDC_ISSUER_URL` need vary per deployment.

## Source

Platform/infrastructure architects. See
[0003-auth-strategy-and-federated-backends](../adrs/0003-auth-strategy-and-federated-backends.md).

## Verification

Code review: no auth code path introduces shared mutable state (e.g.
a session store) that would prevent horizontal or multi-region
scaling.
