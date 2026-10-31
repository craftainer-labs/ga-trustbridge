# s3

RustFS, an S3-compatible object store, for local development without a
real AWS account. MinIO is no longer maintained upstream, so this
template uses RustFS instead.

- Compose file: `compose.yml`
- Image: `rustfs/rustfs:1.0.0-rc.5-glibc`
- API host (from other containers only — see root README's "Don't"): `s3`
- API port (container-internal): `9000`
- Console port (container-internal): `9001`
- Access key / secret key: `rustfsadmin` / `rustfsadmin`
- Endpoint URL: `http://s3:9000`
- Data volume: `s3-data`
- Credentials: defined in `s3.env`, next to this file

## Do

- Use these default credentials for local development only.
- Reach the console at `http://s3:9001`, or the API at `http://s3:9000`,
  from a tool running inside the devcontainer network rather than
  publishing a port to the host: the S3 browsing extension configured in
  `.vscode/settings.json`, the `rc` CLI (RustFS's own, installed by
  `scripts/develop.sh` — run `rc alias set local http://s3:9000
  $RUSTFS_ACCESS_KEY $RUSTFS_SECRET_KEY` once per shell), or Claude's
  `s3-mcp` MCP server (`../../../.mcp.json` — see
  `../../../.claude/README.md`).

## Don't

- Publish this service's ports to the host, or reuse these defaults
  anywhere but local dev.

## Removing this service

Delete this directory and remove its compose file entry from
`.devcontainer/compose.yml`'s `include:` list.
