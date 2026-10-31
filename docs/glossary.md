# Glossary

Domain terminology, so a requirement can use a term precisely instead
of redefining it inline every time it's used.

| Term | Definition |
| --- | --- |
| Hero | The example CRUD resource this template ships with; see `src/app/README.md`. |
| MODE | The single startup-time setting (`dev` / `mock` / `production`) selecting repository, health-check, and auth backends together; see [FR-0021](frs/FR-0021-mode-based-backend-selection.md). |
| CRUD interface | The generic, resource-agnostic list/create/get/update/delete contract every resource is wired into; see [FR-0001](frs/FR-0001-generic-crud-interface.md). |
| Repository | The storage-agnostic persistence abstraction behind a resource's CRUD interface (in-memory or SQLAlchemy). |
| View | The Pydantic schema shaping a resource's request/response representation for a given API version. |
| Problem details | The RFC 9457 `application/problem+json` error response shape used for every error in this app; see [FR-0018](frs/FR-0018-uniform-problem-details-errors.md). |
| RBAC | Role-based access control: granting access based on role claims present in a validated OIDC bearer token. |
| Sunset header | The RFC 8594 HTTP response header announcing the date a deprecated API version will stop being served; see [NFR-0002](nfrs/NFR-0002-deprecation-sunset-headers.md). |
| JWKS | JSON Web Key Set: the public keys an OIDC provider publishes, used to verify bearer token signatures. |
| OIDC | OpenID Connect: the identity layer on top of OAuth2 this app uses for bearer-token authentication. |
| Deprecation | A still-functional but sunset-scheduled API version, as opposed to one already removed. |

## Do

- Add a term here the first time a requirement needs to use it
  precisely and it isn't already common English.
- Keep definitions short — one or two sentences; link to an ADR or
  `src/` `README.md` for anything that needs more.

## Don't

- Define implementation types or classes here — that's a code
  reference, not a domain term.
