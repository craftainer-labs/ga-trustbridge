# 0027. Record product-analytics events with the library's `track()`, privacy-first

## Status

Accepted.

## Context

The plan in `docs/plans/2026-10-bff-wellknown-analytics-owasp.md` adds product
analytics across the template family: a self-hosted, cookieless
[Umami](https://umami.is) in the devcontainer stack
(`.devcontainer/stack/umami/`), and one `track()` contract that the Python and Rust
backends and the BFF all call. This template needs to emit a few server-side domain
events, without turning the API into a tracking surface.

`craftainer-observability` 0.4 ships that contract as its `analytics` extra
(`craftainer_observability.analytics`, its ADR 0003). It validates events, applies the
`ANALYTICS_*` environment, and delivers through adapters (`none`, `log`, `umami`, `otel`)
behind a bounded queue. It cannot see HTTP, so what a request says about tracking
(Global Privacy Control, Do Not Track, consent) has to be handed to it by the
application, through a per-request context it provides.

## Decision

We will:

- Depend on `craftainer-observability[analytics]` 0.4 and call its `track(name, props,
  url=)`. Nothing of the delivery, validation or queueing is reimplemented here, and
  `config.py` carries no `ANALYTICS_*` fields: the library reads that environment itself
  (`ANALYTICS_ENABLED`, `_BACKEND`, `_ENDPOINT`, `_SITE_ID`, `_HOSTNAME`, `_CONSENT`,
  `_RESPECT_GPC`, `_GEO`, `_PROP_KEYS`), so there is one source of truth.
- Wrap each request in the library's `request_context(gpc=, consent=)` from a pure ASGI
  middleware, `app.analytics.AnalyticsContextMiddleware`. `Sec-GPC: 1` or `DNT: 1` sets
  `gpc`, and the library drops the request's events while `ANALYTICS_RESPECT_GPC` is true
  (the default). `Analytics-Consent: 1` sets `consent`, which matters only under
  `ANALYTICS_CONSENT=opt-in`: without it no event is recorded. A browser never sends that
  header by itself, so a front end or BFF that collected consent adds it to its calls.
- Record two sample domain events from Hero's CRUD, `hero.created` and `hero.deleted`,
  by wrapping the resource's `EventSink` in `app.crud_1.analytics_events.AnalyticsEventSink`.
  The wrapped sink publishes first, unchanged, so the event stream and the stats-cache
  invalidation behave as before. Updates and restores are not tracked.
- Send only counts: `powers_count` (an integer) is the one prop, and it is sent only when
  it is on `ANALYTICS_PROP_KEYS` (default empty, so nothing is sent until an operator opts
  in). Names, ids, owners, claims and request headers are never passed to `track()`. The
  event `url` is the resource's path (`/crud/v1/heroes/v2`), never the request's, so query
  strings and record ids cannot leak into it.
- Treat analytics as best effort: `track()` never raises and never blocks, the Umami
  queue drops on overflow, and the lifespan flushes it once on shutdown (off the event
  loop). A request is never slowed or failed by analytics.
- Keep the CNIL audience-measurement exemption as the privacy reference (see
  [NFR-0028](../nfrs/NFR-0028-analytics-privacy.md)), in its stricter reading.

## Consequences

- With `ANALYTICS_BACKEND` unset (`none`) the template behaves as before; the
  devcontainer's `umami.env` selects `umami` and the seeded website.
- Because the library, not this app, validates events, a new event or prop needs three
  things: a conforming name (`^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$`, at most 50 characters),
  a scalar value of at most 200 characters, and its key added to `ANALYTICS_PROP_KEYS`.
- Backend events carry `payload.ip = server` (the library's constant), so Umami stores no
  client address and derives no geography. A deployment cannot get visitor-level analytics
  from this service; page-level analytics belong to the BFF, which sees the browser.
- `Analytics-Consent` is this template's own convention. A deployment that uses
  `opt-in` has to make its front end send it, or no event is ever recorded. The name has
  no `X-` prefix (RFC 6648). Backends trust this header only behind the BFF, which strips
  any inbound copy and sets it from its own consent state; a direct client can set it,
  which is accepted for opt-in mode in development and documented here.
- The wrapper is an indirection around Hero's sink; a resource that wants events does the
  same, with its own `props_for`.
- Umami OSS has no retention setting; the 25-month limit is a manual purge
  (`.devcontainer/stack/umami/purge.sql`), not scheduled here.
