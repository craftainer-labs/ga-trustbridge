# 0013. Use Valkey instead of Redis for the rate-limiter's backing store

## Status

Accepted

## Context

`.devcontainer/stack/redis/compose.yml` pinned `redis:7.4.11-alpine`.
Redis Ltd. relicensed the Redis server away from BSD-3-Clause starting
with 7.4, to a dual RSALv2/SSPLv1 license — source-available, but not
OSI-approved open source, and it carries usage restrictions (e.g. around
offering it as a managed service). This template is meant to be cloned
into arbitrary downstream projects, so a non-OSI-approved license on a
stack service isn't a decision this template should force onto every
project instantiated from it, even though this app's own use of it (an
in-process `Limiter` backend for `app/rate_limit.py`) wouldn't itself
trigger those restrictions.

`docs/plans/2026-09-add-crud-sse.md` raised the same licensing concern
alongside this one but deliberately uses a separate new service (MQTT)
rather than extending this app's Redis/Valkey usage — that's a distinct
decision, out of scope here.

## Decision

**Swap the `redis:7.4.11-alpine` image for `valkey/valkey:8.1.10-alpine`**
— Valkey is the Linux Foundation's BSD-3-Clause fork of Redis (backed by
AWS, Google, Oracle, and others), protocol-compatible with the last
BSD-licensed Redis release. The service keeps the name `redis`
everywhere else in the stack (compose service name, `redis-data` volume,
`REDIS_URL` env var, `redis://` connection scheme, Claude's `redis` MCP
server) since `redis://` is a protocol name, not a product name, and
renaming it would touch a large number of unrelated files (health
checks, config, tests, MCP config) for no functional benefit. Only the
image and the container's own command/healthcheck change, from
`redis-server`/`redis-cli` to `valkey-server`/`valkey-cli` (Valkey ships
its CLI binaries under its own name).

`redis-py` (the Python client used by `app/rate_limit.py` and
`app/health/checks.py`) and Debian's `redis-tools` package (the
`redis-cli` installed by `scripts/develop.sh` for host-side debugging)
are both unaffected: they speak the wire protocol, not a
license-encumbered implementation, and Valkey implements that same
protocol.

## Consequences

The devcontainer stack now has no non-OSI-approved licenses in it.
Downstream, Valkey and Redis can diverge in protocol or command support
over time (their common ancestry is Redis 7.4-era, patched forward
separately); a future feature that depends on a Redis-only or
Valkey-only command would need to check compatibility explicitly, which
this decision doesn't solve for. Nothing in this app's own code changed
— `app.config.Settings.redis_url`, `app.rate_limit`, and
`app.health.checks.RedisHealthCheck` all still refer to "Redis" in
identifiers and docstrings, describing the protocol/interface they
speak rather than the specific server product behind it; that naming
was left as-is rather than renamed to "Valkey" throughout the app, since
this decision covers only the stack's backing service, not those
call sites.

Anyone with a pre-existing `redis-data` volume from the old Redis 7.4
image needs to remove it (`docker volume rm devcontainer_redis-data` or
equivalent) before starting the Valkey image: Valkey can't load an RDB
file in Redis 7.4's newer RDB format ("Can't handle RDB format version
12") and the container will crash-loop until the volume is cleared. The
data is disposable rate-limit-counter state, not anything worth
migrating.
