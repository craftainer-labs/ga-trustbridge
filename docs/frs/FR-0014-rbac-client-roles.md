# FR-0014. Enforce role-based access control from the token's client-role claim

## Status

Implemented

## Description

The system shall grant access to a role-guarded route only if the
bearer token's `resource_access.<client>.roles` claim intersects the
route's required role set, otherwise responding 403 Forbidden.

## Source

Security/compliance. Implemented in `src/app/oidc.py`
(`require_roles`).

## Acceptance criteria

- A valid token lacking any required role receives 403 with a message
  indicating insufficient role, not 401.
- A valid token with at least one required role is granted access.
