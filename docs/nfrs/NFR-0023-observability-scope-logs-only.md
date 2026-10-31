# NFR-0023. Limit observability scope to logs only

## Status

Superseded by [NFR-0025](NFR-0025-observability-scope-logs-traces-and-metrics.md)

## Attribute

Scope constraint / observability.

## Description

Telemetry shall deliberately cover structured logging only; no
distributed tracing or metrics instrumentation is in scope, as a
scope decision to revisit only if a future need requires it.

## Source

Operators/SRE. Documented in `src/app/telemetry.py`'s module
docstring.

## Verification

Code review: no tracing/metrics SDK is added without first revising
this requirement's status.
