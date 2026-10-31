# postgres

PostgreSQL 18, used as the primary application database.

- Compose file: `compose.yml`
- Image: `postgres:18.6-alpine`
- Host (from other containers only — see root README's "Don't"): `postgres`
- Port (container-internal): `5432`
- User / password / database: `app` / `app` / `app`
- Connection URL: `postgresql+asyncpg://app:app@postgres:5432/app`
- Data volume: `postgres-data` (persists across restarts, removed with `docker compose down -v`)
- Credentials: defined in `postgres.env`, next to this file

## Do

- Use these default credentials for local development only.
- Reach this service from a tool running inside the devcontainer network
  rather than publishing the port to the host: the Postgres browsing
  extension configured in `.vscode/settings.json`, the `psql` CLI
  (installed by `scripts/develop.sh`), or Claude's `postgres` MCP server
  (`../../../.mcp.json` — see `../../../.claude/README.md`).

## Don't

- Publish this service's port to the host, or reuse these defaults
  anywhere but local dev.

## Removing this service

Delete this directory and remove its compose file entry from
`.devcontainer/compose.yml`'s `include:` list.
