# FR-0016. Expose an audit endpoint restricted to security/detective roles

## Status

Implemented

## Description

The system shall expose `GET /audit`, restricted to role `security`
or `detective`, returning the caller's subject and granted roles.

## Source

Security/compliance. Implemented in `src/app/controllers/audit.py`.

## Acceptance criteria

- A token without `security` or `detective` receives 403 on
  `GET /audit`.
- A token with `security` or `detective` receives 200 with its own
  subject and roles.
- A `detective`-role token can access both `/audit` and Hero data,
  per `tests/e2e/detective/`.
