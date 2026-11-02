# System design

Application-level view of `src/app/` and the published `craftainer-crud` package: layering, request
flow, the multi-format representation pattern, and auth. For the
infrastructure/deployment view (services, networking, healthchecks), see
[architecture.md](architecture.md). This is a summary that must stay
consistent with [src/app/README.md](../src/app/README.md)'s and
`craftainer_crud/README.md`'s own "Layering" sections
and diagrams, which remain authoritative for the exact import order.

## Layering

The generic CRUD framework lives in the `craftainer-crud` package (its own repository,
a pinned dependency of this one, imported as `craftainer_crud`), laid out as its own
MVC-ish split under one strict, one-directional import order — a lower
layer never imports from a higher one, enforced by `import-linter`'s
`"crud layers"` contract in that package's CI (see
[docs/adrs/0001](adrs/0001-mvc-layering-with-a-generic-crud-interface.md)):

```mermaid
graph LR
    models --> views --> repositories --> interfaces --> controllers
```

- `models/` — the generic SQLAlchemy declarative base, record-lifecycle
  mixins, and shared revision model.
- `views/` — the generic Pydantic view base, converting to/from the ORM
  model purely through `from_attributes` (`ORMView`), plus bulk/revision/
  stats response shapes.
- `repositories/` — a storage-agnostic `Repository` protocol
  (`SQLAlchemyRepository` in `dev`/`production`, `InMemoryRepository`
  under `MODE=mock`).
- `interfaces/` — `CRUDInterface`, generic over a view and a repository;
  adding a resource needs only a model, a view, and a controller, never
  new CRUD code.
- `controllers/` — the generic CRUD router factories, the highest layer
  in this package, may import from any other `craftainer_crud/` subpackage.

`src/app/` is laid out the same MVC-ish way, but holds only
resource-specific code (a resource's own model/views/router) plus
supporting layers, under its own separate `"app layers"` import-linter
contract:

```mermaid
graph LR
    config --> health_checks --> rate_limit --> problem_details --> oidc
    oidc --> models --> views --> web_components --> xml_codec
    xml_codec --> http_headers --> controllers --> crud_1 --> main
```

- `models/` — a resource's own SQLAlchemy model (e.g. `hero.py`), built
  on `craftainer_crud.models`.
- `views/` — a resource's own Pydantic schemas, built on `craftainer_crud.views`.
- `controllers/` — FastAPI routers with no resource of their own
  (audit/mock).
- `crud_1/` — one subpackage per resource, combining its router (built
  from `craftainer_crud.controllers`'s factories) into the single router `main.py`
  mounts.

The generic health check framework (interface, registry, and router
factory backing `/health/live`/`/health/ready`) lives in its own
`craftainer_observability.health` package (from the published `craftainer-observability`), laid out the same MVC-ish way under its
own `"health layers"` contract in that package's CI — no external dependency beyond FastAPI
itself. `app/health_checks.py`'s concrete, service-specific checks
(Postgres, Redis, S3, OIDC) build on that interface and wire into a
registry `main.py` passes to `craftainer_observability.health.router.build_health_router` the
same way it uses `crud_1`'s finished router — see
the `craftainer-observability` package's `README.md` and
[src/app/README.md](../src/app/README.md).

`config.py`/`problem_details.py`/`oidc.py`/
`http_headers.py`/`xml_codec.py`/`web_components.py`/`health_checks.py`/
`main.py` stay flat,
outside any subpackage. See
[src/app/README.md](../src/app/README.md) for what each one does, and
the `craftainer-crud` package's `README.md` "Layering" section for
why `craftainer_crud.controllers` importing back from `app`'s lowest flat modules
doesn't create a cycle between the two packages.

## Request flow

A resource's CRUD routes are one declarative call each
(`build_json_router`/`build_xml_router`/`build_web_router` in
`craftainer_crud/controllers/crud_router.py`), sharing the same underlying
`CRUDInterface`/`CRUDLike` dependency regardless of format:

```mermaid
sequenceDiagram
    participant Client
    participant Controller as app/crud_1/
    participant CRUD as craftainer_crud/interfaces/ (CRUDInterface)
    participant Repo as craftainer_crud/repositories/
    participant Model as app/models/ (Postgres, or in-memory under MODE=mock)

    Client->>Controller: HTTP request
    Controller->>CRUD: get/list/create/update/delete
    CRUD->>Repo: same operation
    Repo->>Model: query / mutate
    Model-->>Repo: row(s)
    Repo-->>CRUD: ORM model(s)
    CRUD-->>Controller: View (from_attributes)
    Controller-->>Client: response (JSON, XML, or HTML — see below)
```

See [src/app/README.md](../src/app/README.md)'s "Example CRUD resource:
Hero" for the same flow worked through a concrete resource.

## Multi-format sibling routers

A resource's XML and HTML/web-form representations are exposed as
sibling routes (`/heroes`, `/heroes/xml`, `/heroes/form`) built by
dedicated router factories, rather than by content negotiation inside
one route — each format shares the same `CRUDLike` dependency, so none
can drift in what data it exposes or what validation it applies. See
[docs/adrs/0005](adrs/0005-multi-format-representations-via-sibling-routers.md)
and `craftainer_crud/controllers/README.md`'s
"Multi-format CRUD"/"Generic CRUD router factories" sections for the
mechanics.

## Auth

Routes opt into auth with `Depends(get_current_claims)`
(`oidc.py`); a route with no such dependency is public. Authorization
(`require_roles(...)`) is enforced server-side, per backend, reading
Keycloak's `resource_access.<client>.roles` claim — never delegated to
a frontend, which can only ever hide UI as a convenience. See
[docs/adrs/0003](adrs/0003-auth-strategy-and-federated-backends.md).

`Settings.mode` (`MODE`: `dev`/`mock`/`production`) drives every
infrastructure fake from one setting instead of per-subsystem toggles:
`MODE=mock` swaps in `InMemoryRepository`, `MockHealthCheck`, and
claims-trusting auth (plus a `POST /mock/token` route), so the full
controller/CRUD stack runs with zero containers. See
[docs/adrs/0006](adrs/0006-mode-driven-fakes-for-infrastructure-free-testing.md)
and [src/app/README.md](../src/app/README.md)'s "MODE (dev / mock /
production)" section.

## trustbridge instance package

`src/trustbridge/` sits beside `src/app/` (it imports `app`; `app` never imports it) and holds the
GitHub App built on this template ([ADR 0028](adrs/0028-trustbridge-architecture-and-instance-layout.md),
[0029](adrs/0029-adapter-plugin-interface.md), [0030](adrs/0030-key-signer-and-github-hosts.md)).
One image runs three processes: the api (`trustbridge.main:app`, which wraps the template's
lifespan), the worker and the reconciler (`python -m trustbridge.worker worker|reconciler`); the
migrate Job is the only process that runs migrations.

| Subpackage | Role |
| --- | --- |
| `webhooks/` | The webhook ingress (limiter, signature check, transactional outbox) and the Argo CD notification route. |
| `worker/` | Shared runtime core: outbox store, Valkey stream, worker, relay, reconciler, fenced leases, schema gate, secret store, tenants, and the ports the two cores need. |
| `github/` | The GitHub core: host configuration, per-host availability (`HostMonitor`), installation tokens, secret/variable/environment writer, check runs and deployments, handler registry, manifest flow. |
| `adapters/` | The adapter plugin interface (I2) and the write-ahead credential core. |
| `signer/` | App-key signers (`file`, `vault-transit`). |

A webhook is verified and committed to Postgres by the api, relayed to the stream by id, and
handled by the worker through the handler registry; a host whose signer, GitHub or OIDC check
fails is `unavailable` (per process, rechecked every `HOST_RECHECK_SECONDS`) and its deliveries
wait in the outbox until it turns `ready`. See [src/trustbridge/README.md](../src/trustbridge/README.md)
for the wiring and each subpackage's `README.md` for detail.

## Do

- Base any change to this document on
  [src/README.md](../src/README.md)/
  [src/app/README.md](../src/app/README.md)/
  the `craftainer-crud` package's README and their subpackage
  `README.md`s — those stay authoritative for exact layering and
  mechanics; this document is the higher-level summary.
- Link to the relevant ADR in [docs/adrs/](adrs/) instead of restating
  its reasoning here.

## Don't

- Duplicate a subpackage `README.md`'s implementation detail here —
  summarize and link instead.
