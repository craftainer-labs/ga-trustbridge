# FR-0024. Stream a resource's CRUD activity over Server-Sent Events

## Status

Implemented

## Description

The system shall let an authenticated, authorized client subscribe to a
resource's create/update/delete/restore activity in real time, as an
opt-in capability of the generic CRUD interface, over a
`GET <prefix>/events` Server-Sent Events stream. A subscriber that
preserves its issued `subscriber_id` and reconnects with it after a
disconnect of no more than one hour shall receive every event published
in the interim, up to 1000 queued events — this guarantee holds only for
the real (non-`MODE=mock`) MQTT-backed deployment, and only for a
reconnect using the same `subscriber_id`; a client that connects fresh
with none gets no replay.

## Source

Feature request, tracked in `docs/plans/2026-09-add-crud-sse.md` (folded
into this requirement and `docs/adrs/0015-mqtt-for-crud-events.md`, and
removed per `docs/plans/README.md`). Depends on
`docs/adrs/0015-mqtt-for-crud-events.md` for the transport/delivery-
guarantee decision.

## Acceptance criteria

- `GET <prefix>/events` exists for a resource whose router was built with
  `event_source_dependency` set (Hero: `/crud/v1/heroes/v2/json/events`),
  gated by the same role dependency as that resource's plain `GET` list
  route.
- The stream's first frame carries a `subscriber_id` the client can pass
  back (`?subscriber_id=` query param, or the standard `Last-Event-ID`
  header) on reconnect.
- Every `create`/`update`/`update_many`/`delete`/`delete_many`/`restore`/
  `restore_many` on that resource publishes one event carrying the
  resource name, record id, action, a JSON snapshot of the record, and a
  timestamp.
- A subscriber that disconnects and reconnects within one hour using the
  same `subscriber_id`, against the real (non-`MODE=mock`) deployment,
  receives every event published in the interim, up to 1000 queued
  events — proven by `tests/integration/crud_1/heroes/
  test_heroes_v2_events.py` against the real Mosquitto service.
- A subscriber that connects with no `subscriber_id` (or discards a
  previously-issued one) receives no replay of events published before
  it connected — this is a deliberate scope limit, not a defect.
- Under `MODE=mock`, the same route streams events via
  `app.interfaces.base.InMemoryEventSink` with no external service
  required, but with no delivery guarantee across a disconnect.
- The stream sends a periodic keep-alive comment (`Settings.
  sse_keepalive_seconds`, default 15s) so an idle connection isn't closed
  by an intermediary proxy/load balancer, and ends when the client
  disconnects.
