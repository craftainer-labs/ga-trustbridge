# redis

[Valkey](https://valkey.io/) 8, for caching, background-job queues, or
pub/sub. Valkey is the Linux Foundation's BSD-3-Clause fork of Redis,
speaking the same wire protocol; this service (and directory) kept the
name `redis` since that's what the rest of the stack — the `REDIS_URL`
env var, the `redis://` connection scheme, Claude's `redis` MCP server —
already calls it, and `redis://` is a protocol name, not a product name.
It replaced actual Redis because Redis Ltd. relicensed the Redis server
away from BSD-3-Clause starting with 7.4 (dual RSALv2/SSPLv1 — source-
available but not OSI-approved, with usage restrictions this template
shouldn't force on every project instantiated from it); see
`docs/adrs/0013-valkey-over-redis-licensing.md`.

- Compose file: `compose.yml`
- Image: `valkey/valkey:8.1.10-alpine`
- Host (from other containers only — see root README's "Don't"): `redis`
- Port (container-internal): `6379`
- Connection URL: `redis://redis:6379/0`
- Data volume: `redis-data` (append-only persistence enabled)

## Do

- Reach this service from a tool running inside the devcontainer network
  rather than publishing the port to the host: the `redis-cli` CLI
  (installed by `scripts/develop.sh` from Debian's `redis-tools` package —
  run `redis-cli -h redis`; it speaks the same wire protocol Valkey
  implements, so it works against this service unchanged), or Claude's
  `redis` MCP server (`../../../.mcp.json` — see
  `../../../.claude/README.md`).

## Don't

- Publish this service's port to the host, or reuse these defaults
  anywhere but local dev.

## Removing this service

Delete this directory and remove its compose file entry from
`.devcontainer/compose.yml`'s `include:` list.
