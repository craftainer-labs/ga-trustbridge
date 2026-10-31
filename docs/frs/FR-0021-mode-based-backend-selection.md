# FR-0021. Select backend implementations from a single MODE setting

## Status

Implemented

## Description

The system shall use one startup-time `MODE` setting (`dev` / `mock`
/ `production`) to control: debugger enablement (`dev` only),
repository backend (in-memory vs. SQLAlchemy/Postgres), health-check
backend (mock vs. real), OIDC token verification (trust-claims vs.
real JWKS verification), and whether `/mock/token` is mounted
(`mock` only).

## Source

Developers; operators. Implemented in `src/app/config.py`,
`src/app/main.py`.

## Acceptance criteria

- Changing only `MODE` switches repository, health-check, and auth
  backends consistently, with no other configuration required.
- `MODE=dev` enables the debugger; `MODE=mock`/`production` do not.
