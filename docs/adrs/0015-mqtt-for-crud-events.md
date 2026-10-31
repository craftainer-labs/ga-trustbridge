# 0015. Use MQTT (persistent sessions, QoS 1) for the CRUD event stream, not Redis

## Status

Accepted

## Context

The generic CRUD interface (`docs/adrs/0001-mvc-layering-with-a-generic-
crud-interface.md`) had no way for a client to subscribe to a resource's
create/update/delete/restore activity in real time — the only option was
polling `GET <prefix>` on an interval. The feature request was for a
`GET <prefix>/events` Server-Sent Events stream, as an opt-in capability
of the generic interface (the same shape as the existing opt-in hooks,
`OwnerScope`/`RevisionSink`), demonstrated on Hero.

Plain SSE has no built-in exactly-once/at-least-once semantics beyond a
`Last-Event-ID` header a client may resend on reconnect — that alone
doesn't stop a subscriber from silently missing events published while
its connection was briefly down (a browser tab backgrounded, a laptop
sleeping, a network blip). A hard requirement was that a subscriber must
not miss events across a *brief* disconnect, which ruled out anything
fire-and-forget on the publish side.

Three transports were considered for the publish side this SSE stream
rides on:

- **Redis pub/sub**, using this app's existing Redis/Valkey service
  (`docs/adrs/0013-valkey-over-redis-licensing.md`). Rejected: pub/sub is
  fire-and-forget with no replay — a subscriber disconnected when a
  message is published never sees it, which is exactly the guarantee
  this feature needs.
- **Redis Streams**, also on the existing service. Would keep every
  transport on one already-running image, but doesn't remove the
  licensing question that service was already flagged for (Valkey's swap
  resolves the *current* Redis-shaped workload; the Streams API doesn't
  change that project's own licensing status) and is a heavier lift on
  top of a service that already needed a separate remediation. Streams'
  own delivery semantics (consumer groups, `XACK`, pending-entries lists)
  would also have to be reimplemented in application code to get anything
  like a per-subscriber persistent queue.
- **Database polling** for new revision-log rows. Rejected on latency
  (real-time means sub-second, not poll-interval) and load (every open
  SSE connection would need its own polling loop against Postgres).

## Decision

**We will use MQTT** (Eclipse Mosquitto, a new devcontainer stack
service at `.devcontainer/stack/mqtt/`) as the transport, with the
delivery guarantee built entirely from MQTT's own persistent-session
mechanism rather than anything SSE itself provides:

- Each SSE subscriber is issued a stable `subscriber_id` on first
  connect (returned as the stream's first frame) that the client passes
  back (`?subscriber_id=` or `Last-Event-ID`) on reconnect.
- The server maps that `subscriber_id` to a **persistent MQTT session**:
  `clean_session=False` with a client id derived from `subscriber_id`
  (`app.interfaces.base.MQTTEventSource`), subscribed at **QoS 1** to
  `crud-events/<resource>`. Because the session is persistent, the
  broker queues messages published while that client id is disconnected
  and delivers them once the same client id reconnects — this is what
  makes "briefly offline, no missed events" hold.
- The broker-side queue per persistent session is bounded
  (`.devcontainer/stack/mqtt/mosquitto.conf`): `max_queued_messages 1000`
  and `persistent_client_expiration 1h` — a session that never
  reconnects within an hour is dropped along with whatever it queued.
  (MQTT v5's per-message `message_expiry_interval` property, named in
  the plan this ADR was written from, isn't a `mosquitto.conf` broker
  setting; `persistent_client_expiration` is Mosquitto's actual
  broker-wide equivalent for aiomqtt's default v3.1.1 protocol.) This
  isn't unbounded retention like a log — it protects a subscriber gone
  for a bounded window, not forever.
- This guarantee is scoped to **QoS-1, persistent-session reconnects with
  the same `subscriber_id`** — a client that discards its `subscriber_id`
  and connects fresh gets no replay, by design (there's no infinite
  backlog).

**Licensing**: Eclipse Mosquitto is dual EPL-2.0/EDL-1.0, both
OSI-approved — this was never in question the way Redis's own licensing
was (`docs/adrs/0013-...md`), and this feature deliberately doesn't touch
that existing Redis/Valkey service at all; it backs `app.rate_limit`'s
`Limiter` only, an unrelated workload.

**App-side client library**: `aiomqtt` (MIT-licensed, asyncio-native
wrapper over `paho-mqtt`, itself dual EPL-2.0/EDL-1.0) — preferred over
raw `paho-mqtt`'s callback-based API since the rest of this codebase's
I/O is async throughout.

**`MODE=mock`**: `app.interfaces.base.InMemoryEventSink`, an
`asyncio.Queue` fan-out per resource, built once and shared (mirroring
`app.interfaces.dependency.build_repository_provider`'s
`InMemoryRepository`) — so the whole stack keeps working with zero
containers under `MODE=mock`. This path is necessarily best-effort: no
broker, no persistent session, no queued replay for a disconnected
subscriber. The delivery guarantee above applies only to the real
MQTT-backed path.

**`EventSink` scope is broader than `RevisionSink`'s**: it fires on
`create`/`update`/`update_many`/`delete`/`delete_many` (the same five
`RevisionSink` covers) **and** `restore`/`restore_many` — a subscriber
watching a resource's real-time activity cares about a record becoming
visible again, unlike the revision log, which stays scoped to the
original five for its own, separate reasons (`app.interfaces.base.
RepositoryRevisionSink`'s own docstring).

## Consequences

A resource opts into real-time streaming with one additional
constructor argument (`CRUDInterface(..., events=EventSink)`) and one
additional router-factory argument
(`build_json_router(..., event_source_dependency=...)`), the same shape
as every other opt-in hook — Hero demonstrates both. Any future resource
gets `GET <prefix>/events` for free once wired up the same way.

The cost: a fourth stack service (`.devcontainer/stack/mqtt/`) with its
own healthcheck, `depends_on:` entry, and persistence volume — one more
container the devcontainer, CI, and any future deployment must run.
`interfaces/base.py` now imports `aiomqtt` directly, a new third-party
dependency `app.interfaces` didn't previously need. The delivery
guarantee's scope is real but narrow: it only helps a client that
actually preserves and resends its `subscriber_id`; a naively-written
client that reconnects with none loses events with no error raised
anywhere — this is documented in `docs/frs/FR-0024-crud-event-stream.md`
and `app.interfaces.base.EventSource`'s own docstring specifically so it
isn't mistaken for an unbounded guarantee. `MODE=mock`'s
`InMemoryEventSink` gives no such guarantee at all, so a test or local
run against it can't be used to validate the delivery guarantee itself —
only `tests/integration/crud_1/heroes/test_heroes_v2_events.py`, run
against the real Mosquitto service, actually proves it.
