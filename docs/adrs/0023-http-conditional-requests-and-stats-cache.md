# 0023. Add ETags to per-user reads and an optional shared cache for `/stats` and `/predict`

## Status

Accepted

## Context

Phase 6 of the performance plan asked whether to cache. The constraint is
[`0011`](0011-owner-scoped-crud-example-resource.md): resources can be
owner-scoped, so a shared cache of lists or records would need the user
and roles in every key, and one mistake there leaks data. On 1 vCPU, a
Redis round trip also costs about as much as the indexed query it would
replace. Whole-table aggregates (`/stats`, `/predict`) are the opposite
case: the most expensive reads, and not per-record.

CRUD events are already published on every write
([`0015`](0015-mqtt-for-crud-events.md)), and multi-format parity applies
to anything touching responses
([`0005`](0005-multi-format-representations-via-sibling-routers.md),
[`0017`](0017-linear-forecast-and-json-only-events-for-crud-stats.md)).

## Decision

We will use two mechanisms, split by whether the data is per-user.

**1. Conditional requests for per-user reads (no shared cache).**
Single-record `GET`s get an `ETag` derived from the record's id and
`updated_at` (or latest revision); list `GET`s get a weak `ETag` over
the page's ids and `updated_at` values. `If-None-Match` yields `304`
with no body. Responses carry `Cache-Control: private, no-cache`:
browsers may revalidate, shared proxies never store. The same `ETag`s
enable `If-Match` on `PATCH`/`DELETE` (`412` on mismatch), which gives
optimistic concurrency control so concurrent edits cannot silently
overwrite each other. JSON, XML and web routes behave the same. The query
still runs, so the saving is serialization and bandwidth only.

**2. Optional server-side cache for `/stats` and `/predict`.**
`craftainer_tool_crud` defines a small `StatsCache` protocol
(`get`, `set`, `invalidate` by resource); the router factories take an
optional cache and `crud` never depends on Redis. `app.cache` implements
it with `redis.asyncio` (already a dependency; `rediss://` TLS works as
for the rate limiter) and with an in-process variant under `MODE=mock`.
It is off unless `STATS_CACHE_TTL_SECONDS` is set, in which case routes
behave exactly as before.

*Key rules.* The key is a SHA-256 over the route kind, the response
schema (so a deprecated API version with another shape never shares an
entry with the current one), the caller scope and the normalized query
parameters; hashing keeps an owner value out of Redis in clear text. The
scope is one `shared` value, unless the resource has a read-scoped
`OwnerScope`, in which case it is one scope per owner value. The cache
implementation adds a version namespace (`APP_VERSION`, else the
installed distribution's version), so a canary release with a different
response shape never reads the other version's entries.

*Why roles are not in the key.* The route's `read_roles` dependency runs
before the cache is consulted and decides whether a caller may read,
never what they read. Rows visible to a caller depend only on ownership
scope, so adding roles would only fragment the cache and add a way to get
the key wrong.

*Invalidation.* Event-driven, with a TTL net: each replica runs a
lifespan task that subscribes to the resource's CRUD event stream and
drops the resource's entries on every event (across app versions, since a
write by either version makes both stale), and once on every
(re)subscribe to cover events missed while disconnected. The TTL bounds
whatever that misses: a read racing a write, a disconnected replica, a
Redis outage during invalidation. A Redis outage never fails a request;
the cache degrades to a miss and logs a warning.

**Rejected: `fastapi-cache2`.** It decorates endpoints, so the key is
built from the function arguments and a response, and fits neither the
caller-scope rule (which must come from the CRUD object's ownership
configuration, not from request arguments) nor `crud`'s requirement to
stay free of Redis and to be published as a separate package
([`0020`](0020-extract-crud-and-observability-into-published-packages.md)).
It also has no hook for event-driven invalidation across replicas, and
it fails the maintenance check Phase 6.2 asked for: its last release is
0.2.2 (July 2024), and its Redis extra pins `redis<5`, while this repo
locks redis-py 8.1.

Not done: caching list or record `GET`s, and putting roles or user IDs
in shared keys.

## Consequences

Easier: repeated `/stats` and `/predict` calls skip the whole-table
aggregate; clients can revalidate cheaply and avoid lost updates; keys
cannot leak owner-scoped data because scope comes from the CRUD
object, not from per-route code.

Harder: cached aggregates can be stale for up to the TTL when
invalidation fails; each replica needs the invalidation task and a
stable subscriber ID; `If-Match` makes clients handle `412`. The `304`
saves bandwidth but not the query. Not measured: the
cache's CPU ms/request and p99 on a `/stats`-heavy mix (cache on versus
off) were not A/B'd, and no measurement was planned for it. The ETag revalidation
saving was not measured either: the revalidate task is opt-in with
weight 0, so the standard runs never exercise it. Both are
correctness-and-bandwidth features here, not accepted on a CPU win.
