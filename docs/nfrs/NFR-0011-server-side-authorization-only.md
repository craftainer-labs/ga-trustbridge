# NFR-0011. Enforce authorization entirely server-side

## Attribute

Security architecture.

## Description

Authorization shall be enforced entirely server-side, independently
per backend; the frontend shall be trusted only to obtain and attach
a bearer token, never to perform enforcement itself.

## Source

Security/compliance. See
[0003-auth-strategy-and-federated-backends](../adrs/0003-auth-strategy-and-federated-backends.md).

## Verification

Code review: no route may rely on client-supplied state to decide
access; role checks live in `src/app/oidc.py`/controllers, never in
`web_components.py`.
