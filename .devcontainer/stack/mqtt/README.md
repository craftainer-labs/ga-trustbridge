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
- Image: `eclipse-mosquitto:2.0.22`
- Host (from other containers only — see root README's "Don't"): `mqtt`
- Port (container-internal): `8883`, TLS only. There is no plaintext listener
  and `allow_anonymous false`, so the dev broker behaves as the production
  guardrails in `src/app/config.py` require (ADR 0028).
- Config: `mosquitto.conf`, bind-mounted read-only — persistence enabled
  (`persistence true` + the `mqtt-data` volume), the TLS listener,
  password auth, and the persistent-session
  queue bounds (`max_queued_messages`, `persistent_client_expiration`,
  `max_inflight_messages`) documented inline and in the ADR's "Delivery
  guarantee" section.
- Credentials: `mqtt.env` (`MQTT_USERNAME`/`MQTT_PASSWORD`, dev-only values),
  loaded into this service, which builds the broker's password file from it at
  every start, and into `myapp` through `../../compose.instance.yml`.
- Certificate: the one-shot `mqtt-tls-cert` service writes a self-signed
  certificate (SAN `DNS:mqtt`) into the `mqtt-tls` volume once. The broker
  serves it; `myapp` mounts the volume read-only at `/mqtt-tls` and trusts it
  through `MQTT_CA_FILE=/mqtt-tls/mqtt.crt`.
- Data volume: `mqtt-data` (required for a persistent session's queued
  messages to survive a broker restart).

Changing any of this needs a devcontainer rebuild (or a `docker compose up`
of the stack); the running containers keep the old broker.

## Do

- Reach this service from a tool running inside the devcontainer network
  rather than publishing the port to the host — `mosquitto_pub`/
  `mosquitto_sub` (shipped in the broker image itself; run them via
  `docker compose exec mqtt sh -c 'mosquitto_pub -h mqtt -p 8883
  --cafile /tls/mqtt.crt -u "$MQTT_USERNAME" -P "$MQTT_PASSWORD" -t <topic>
  -m <message>'`; the broker only accepts TLS and credentials) for manual
  publish/subscribe testing.
- Delete the `mqtt-tls` volume (`docker compose down -v` removes it with the
  rest) to regenerate the certificate if its pair is ever stale or mismatched;
  `mqtt-tls-cert` only writes one when a file is missing or empty.

## Don't

- Publish this service's port to the host, or reuse `mosquitto.conf`, the
  `mqtt.env` credentials or the self-signed certificate anywhere but local dev
  — see the root README's "Don't" and this directory's own `compose.yml`
  comment.
- Point a client at the broker without TLS or credentials: it is refused.
  `mosquitto_pub`/`mosquitto_sub` need `-p 8883 --cafile /tls/mqtt.crt -u ... -P ...`.

## Removing this service

Delete this directory and remove its compose file entry from
`.devcontainer/compose.instance.yml`'s `include:` list, and from the `myapp`
service there the matching `depends_on:` entry, the `./stack/mqtt/mqtt.env`
`env_file:` entry, the `mqtt-tls:/mqtt-tls:ro` volume and the `MQTT_*`
environment entries (the file and the volume would no longer exist) — this also removes the real
(non-`MODE=mock`) backend for `craftainer_crud.interfaces.base.MQTTEventSink`/
`MQTTEventSource`, so a resource using `event_source_dependency` would
need `MODE=mock` or a different broker configured via
`Settings.mqtt_host`/`mqtt_port`.
