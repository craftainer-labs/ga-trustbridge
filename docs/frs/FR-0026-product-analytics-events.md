# FR-0026. Record sample product-analytics events and honour the request's tracking signals

## Status

Implemented

## Description

The system shall record a `hero.created` event when a hero is created and a
`hero.deleted` event when one is deleted, through the vendor-neutral `track()` of
`craftainer-observability`'s `analytics` extra, to the backend named by
`ANALYTICS_BACKEND`. It shall not record an event for a request that sends
`Sec-GPC: 1` or `DNT: 1` while `ANALYTICS_RESPECT_GPC` is true (the default), and, when
`ANALYTICS_CONSENT=opt-in`, shall not record one for a request without
`Analytics-Consent: 1`. Analytics shall never slow down, block or fail a request.

## Source

Product and privacy: the plan in
`docs/plans/2026-10-bff-wellknown-analytics-owasp.md` (track AN). See
[0027-product-analytics-with-track](../adrs/0027-product-analytics-with-track.md) and
[NFR-0028](../nfrs/NFR-0028-analytics-privacy.md). Implemented in
`src/app/analytics.py`, `src/app/crud_1/analytics_events.py`,
`src/app/crud_1/heroes/heroes_v2.py`, `src/app/main.py`.

## Acceptance criteria

- Creating a hero records one `hero.created`, and deleting one records one
  `hero.deleted`, each with `url` `/crud/v1/heroes/v2`; updating or restoring a hero
  records nothing. Both events still reach the hero event stream (`GET .../events`).
- The only prop is `powers_count`, an integer, and it is sent only when it is on
  `ANALYTICS_PROP_KEYS`; no event contains a hero's name, id, owner, a token or a header.
- A request with `Sec-GPC: 1` or `DNT: 1` records no event while `ANALYTICS_RESPECT_GPC`
  is true; with it false the signal is ignored. Any other value (`0`, `yes`) is not a signal.
- With `ANALYTICS_CONSENT=opt-in`, a request records an event only with
  `Analytics-Consent: 1`, and a GPC/DNT signal still wins over consent. With `implied`
  (the default) the consent header changes nothing.
- With `ANALYTICS_BACKEND=none` (the default) or `ANALYTICS_ENABLED=false`, nothing is
  recorded and responses are unchanged.
- The event is recorded when the resource's `EventSink` publishes, which `craftainer_crud`
  does after the repository has committed the create or delete (the same moment as the hero
  event stream), so a rolled-back write records nothing. Delivery is best-effort.
- An unreachable analytics backend does not change a response's status or latency; the
  event is dropped.
