# NFR-0015. Redact unhandled-exception detail outside dev mode

## Attribute

Security.

## Description

An unhandled exception shall return 500 with a generic detail
("Internal Server Error") under `MODE=mock`/`production`; only
`MODE=dev` shall include the actual exception message, to avoid
leaking internals in any environment reachable by real users.

## Source

Security/compliance; operators. Implemented in
`src/app/problem_details.py`.

## Verification

Unit tests assert the generic message under mock/production and the
real exception message under dev.
