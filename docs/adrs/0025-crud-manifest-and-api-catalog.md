# 0025. Publish the CRUD manifest and an api-catalog

## Status

Accepted

## Context

The template family gives clients (the
Python and TypeScript SDKs, the React UI layer) one machine-readable
description of every CRUD resource, the CRUD manifest (contract I1,
specified in `craftainer-crud`'s `docs/manifest.md` and its ADR 0002).
`craftainer-crud` 0.2.0 builds it from the same field specs and router
options the routers use and serves it; this template has to register its
routers and mount the routes. The distributions were also renamed from
`craftainer-tool-crud` / `craftainer-tool-observability` to
`craftainer-crud` / `craftainer-observability` (import names
`craftainer_crud`, `craftainer_observability`), with no shim for the old
names, so every import changed in the same step.

Discovery needs a stable, standard entry point. An unregistered
`/.well-known/crud` was rejected in favour of RFC 9727's
`/.well-known/api-catalog` linkset, which points at the manifest with a
`describedby` link carrying the manifest's profile URI.

## Decision

- Depend on `craftainer-crud>=0.2.0,<0.3` and
  `craftainer-observability>=0.2.0,<0.3`. `craftainer-crud-sdk>=0.1.0,<0.2`
  is a `dev` dependency only: the e2e tests use it as a real client.
- Every resource router passes `manifest=ManifestOptions(...)` (name, API
  version, title, model, the role names) to its factory; all register in
  `app.crud_1.wiring.manifest_registry`. The role-name tuples are defined
  once and feed both `require_roles` and the manifest.
- `main.py` mounts `crud_manifest_router` at `/api/_crud` (so
  `GET /api/_crud/manifest`) and `api_catalog_router` at the root
  (`GET /.well-known/api-catalog`, no authentication).
- The manifest needs the same roles as reading each resource: the library
  runs each resource's own read-role dependency per request, so a caller
  refused there does not see the resource, and an unauthenticated caller
  sees only `public_read` resources (none in this template). ETag and
  `Cache-Control: private, no-cache` come from the library; nothing here
  re-implements them.
- The api-catalog mount is the one `include_router` with `prefix=""`: the
  route's path is fixed by RFC 9727, so there is no segment for the mount
  to name (an exception to the rule in `src/app/crud_1/README.md`).

## Consequences

- Clients discover the API from the origin alone and read each caller's
  resources, paths and limits from the manifest instead of hardcoding them.
- The manifest cannot drift from the routers, since they register
  themselves; adding a resource means passing `manifest=` in its factory call.
- With `MODE=production` the OpenAPI route is off, but the catalog still
  lists `/openapi.json` as `service-desc` (the library has no way to omit it).
- Dependency ranges (not exact pins) for the two packages let Renovate
  and `uv lock` take 0.2.x patch releases; the lockfile still fixes the
  tested versions.
