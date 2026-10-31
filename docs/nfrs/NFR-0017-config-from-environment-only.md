# NFR-0017. Source all configuration from the environment, never hardcode secrets

## Attribute

Operational / security constraint.

## Description

`Settings` shall read exclusively from process environment variables
(no `.env` file read by the app itself), assembling composed values
(e.g. `database_url` from `postgres_*` pieces) at runtime. Real
secrets shall never be hardcoded in source.

## Source

Operators/SRE; security/compliance. Implemented in
`src/app/config.py`; documented in `docs/TEMPLATE.md`'s "Don't" and
`src/app/README.md`'s "Configuration"/"Don't".

## Verification

Code review; no `.env` file is read by application code, and secrets
live under `.secrets/` (never committed) per the root README.
