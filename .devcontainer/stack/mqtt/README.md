# mqtt

[Eclipse Mosquitto](https://mosquitto.org/), the MQTT broker backing
`craftainer_crud.interfaces.base`'s `MQTTEventSink`/`MQTTEventSource` (the
generic CRUD interface's opt-in real-time event stream — see
`docs/adrs/0015-mqtt-for-crud-events.md`). Mosquitto is dual
EPL-2.0/EDL-1.0 licensed, both OSI-approved, unlike this stack's Redis/
Valkey service was before its own licensing swap (`docs/adrs/
0013-valkey-over-redis-licensing.md`) — a concern this new service never
had to begin with, since it's introduced fresh here.

- Compose file: `compose.yml`
- Image: `eclipse-mosquitto:2.0.20`
- Host (from other containers only — see root README's "Don't"): `mqtt`
- Port (container-internal): `1883`
- Config: `mosquitto.conf`, bind-mounted read-only — persistence enabled
  (`persistence true` + the `mqtt-data` volume), no auth/TLS (a
  local-dev-only default, see "Don't" below), and the persistent-session
  queue bounds (`max_queued_messages`, `persistent_client_expiration`,
  `max_inflight_messages`) documented inline and in the ADR's "Delivery
  guarantee" section.
- Data volume: `mqtt-data` (required for a persistent session's queued
  messages to survive a broker restart).

## Do

- Reach this service from a tool running inside the devcontainer network
  rather than publishing the port to the host — `mosquitto_pub`/
  `mosquitto_sub` (shipped in the broker image itself; run them via
  `docker compose exec mqtt mosquitto_pub -h localhost -t <topic> -m
  <message>`) for manual publish/subscribe testing.

## Don't

- Publish this service's port to the host, or reuse `mosquitto.conf`'s
  no-auth/no-TLS defaults anywhere but local dev — see the root README's
  "Don't" and this directory's own `compose.yml` comment.

## Removing this service

Delete this directory and remove its compose file entry from
`.devcontainer/compose.instance.yml`'s `include:` list (and the `myapp` service's
matching `depends_on:` entry there) — this also removes the real
(non-`MODE=mock`) backend for `craftainer_crud.interfaces.base.MQTTEventSink`/
`MQTTEventSource`, so a resource using `event_source_dependency` would
need `MODE=mock` or a different broker configured via
`Settings.mqtt_host`/`mqtt_port`.
