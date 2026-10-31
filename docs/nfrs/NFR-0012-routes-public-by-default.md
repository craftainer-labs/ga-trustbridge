# NFR-0012. Keep routes public unless auth is explicitly declared

## Attribute

Security posture.

## Description

Only routes that explicitly declare `Depends(get_current_claims)` (or
a role dependency) shall require authentication; every other route is
public by default. This is a deliberate, documented default rather
than an oversight, to make protection an opt-in, visible choice at
each route.

## Source

Security/compliance; developers maintaining the template. Documented
in `src/app/README.md`'s "OIDC/auth" section.

## Verification

Code review: a new route's exposure is decided by whether it declares
the auth dependency, not by a global default that could silently
protect or expose it.
