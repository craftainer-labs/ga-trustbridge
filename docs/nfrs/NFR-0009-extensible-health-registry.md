# NFR-0009. Make adding a new dependency health check a two-step addition

## Attribute

Maintainability.

## Description

Adding a health check for a new external dependency shall require
only implementing the `HealthCheck` protocol and registering it in
`get_health_registry`, with no other wiring.

## Source

Developers maintaining the template. See
[0001-mvc-layering-with-a-generic-crud-interface](../adrs/0001-mvc-layering-with-a-generic-crud-interface.md),
`src/app/health/README.md`.

## Verification

Code review; demonstrated by the existing Postgres/Redis/S3/OIDC
checks all following the same two-step pattern.
