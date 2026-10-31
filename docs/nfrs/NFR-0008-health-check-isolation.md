# NFR-0008. Isolate health check failures from each other

## Attribute

Availability / resilience.

## Description

Each concrete health check shall catch its own dependency's narrow
exception type and report `healthy=False` with a `detail` message,
rather than letting an exception propagate and take down the entire
readiness response.

## Source

Operators/SRE. Documented in `src/app/health/README.md`'s "Don't"
section; implemented in `src/app/health/checks.py`.

## Verification

Unit tests simulate a failing dependency (e.g. Postgres connection
error) and assert `/health/ready` still returns a well-formed 503
response rather than a 500.
