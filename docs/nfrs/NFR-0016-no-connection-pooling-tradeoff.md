# NFR-0016. Accept no async connection pooling as a documented trade-off

## Status

Superseded by [NFR-0026](NFR-0026-connection-pooling-per-deployment.md)

## Attribute

Performance / architecture constraint.

## Description

The async database engine shall use `NullPool` deliberately, because
pooled asyncpg connections are tied to the event loop that opened
them and cannot survive tests mixing event loops. This trades away
connection reuse; if traffic requires pooling, it should be added
externally (e.g. PgBouncer) rather than by reintroducing an in-process
pool.

## Source

Developers; performance/operations. Implemented and documented in
`src/app/models/base.py`.

## Verification

Code review against the documented rationale before changing the
pool class, plus `tests/perf/` (`NFR-0024`) as an automated signal that
would catch a pooling-related latency/throughput regression, even if it
doesn't verify the trade-off's rationale itself.
