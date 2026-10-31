# NFR-0007. Propagate router-level headers into hand-built responses

## Attribute

Correctness constraint.

## Description

Any router-level dependency that sets response headers (e.g. the
Sunset/Deprecation headers of [NFR-0002](NFR-0002-deprecation-sunset-headers.md))
shall be explicitly merged into any hand-constructed `Response` or
`RedirectResponse` (XML bodies, form redirects, JS bodies), since
FastAPI does not merge dependency-set headers into those automatically.

## Source

Developers maintaining the template. Implemented via
`_with_dependency_headers` in `src/app/controllers/crud_router.py`;
documented in `src/app/controllers/README.md`. This was previously a
live bug on v1 XML/web routes.

## Verification

Automated tests assert Sunset/Deprecation headers are present on v1
XML, form, and JS responses, not only on plain JSON responses.
