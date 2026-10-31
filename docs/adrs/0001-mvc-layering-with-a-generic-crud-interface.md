# 0001. Use an MVC layering with a generic CRUD interface, backed by a storage-agnostic repository

## Status

Accepted

## Context

The template needed a concrete example of a database-backed resource
(Hero), a persistence layer using SQLAlchemy against Postgres, and a
health check endpoint that reports on every external service the app
depends on (Postgres, Redis, S3, the OIDC provider) in a form Kubernetes
liveness/readiness probes can use. It also needed a repeatable pattern:
a template's job is to make the *next* resource cheap to add, not just
to demonstrate one.

Two designs were on the table for the CRUD layer. The first: one CRUD
class per resource, hand-written against that resource's SQLAlchemy
model and Pydantic schema — simple to read, but every new resource
duplicates the same five operations with only the types changed. The
second: a single generic CRUD interface, parameterized by a Pydantic
view and a storage-agnostic repository, so a new resource needs only a
model, a view, and a thin router — no new CRUD code. The second was
chosen deliberately over the "three similar lines is better than a
premature abstraction" default in `CLAUDE.md`, because a template's
purpose is specifically to make the *n+1*th resource cheap; that
argument doesn't hold for a one-off app.

## Decision

We will lay `src/app/` out as an MVC-ish split — `models/` (SQLAlchemy),
`views/` (Pydantic), `controllers/` (FastAPI routers) — plus two
supporting layers: `repositories/` (a storage-agnostic `Repository`
protocol, with `SQLAlchemyRepository` as its concrete implementation)
and `crud/` (`CRUDInterface`, generic over a view and a repository).
`CRUDInterface` converts to/from the ORM model purely through the view's
`from_attributes` support (`app.views.base.ORMView`), never by importing
a specific model class — so `crud/base.py` has no resource-specific code
at all. Adding a resource means: an `IdentifiedBase` subclass in
`models/`, an `ORMView` subclass (plus `*Create`/`*Update`) in `views/`,
and a controller that builds `CRUDInterface(schema=View,
repository=SQLAlchemyRepository(session, Model))` per request — see
`src/app/README.md`'s "Example CRUD resource: Hero".

```mermaid
graph TD
    Controller["controllers/ (FastAPI router)"] --> CRUD["crud/ (CRUDInterface)"]
    CRUD --> View["views/ (Pydantic, ORMView)"]
    CRUD --> Repository["repositories/ (Repository protocol)"]
    Repository --> Model["models/ (SQLAlchemy)"]
    Repository -->|SQLAlchemyRepository| DB[(Postgres)]
    Controller -.->|Depends| HealthReg["health/ (HealthRegistry)"]
    HealthReg --> Postgres[(Postgres)]
    HealthReg --> Redis[(Redis)]
    HealthReg --> S3[(S3)]
    HealthReg --> OIDCProvider[(OIDC provider)]
```

We will enforce a strict, one-directional import order between these
layers (`config` → `oidc` → `models` → `views` → `repositories` →
`crud` → `health` → `controllers` → `main`) with `import-linter`'s
`layers` contract, run in CI alongside `ruff`/`mypy`.

```mermaid
graph LR
    config --> oidc --> models --> views --> repositories --> crud --> health --> controllers --> main
```

This is what keeps
`crud/base.py` and `repositories/sqlalchemy.py` genuinely generic: a
lower layer that could import a higher one would eventually grow a
resource-specific special case, and the contract fails the build before
that lands rather than catching it in review.

We will apply the same "storage-agnostic interface, one concrete
implementation" shape to health checks: `app.health.base.HealthCheck` is
a protocol: any external service (Postgres, Redis, S3, OIDC — or a
message queue, another API, ... later) implements it and registers
itself with `HealthRegistry`, which `/health/ready` runs concurrently.
`/health/live` stays a separate, dependency-free route, so Kubernetes
can point a liveness probe at one and a readiness probe at the other
without the liveness probe depending on every downstream service being
up.

## Consequences

Adding a new CRUD resource is now three small, mechanical files instead
of a bespoke service class — the intended payoff, and the reason this
deviates from `CLAUDE.md`'s usual anti-abstraction default. The cost is
one extra level of indirection when reading any single resource's code:
following a request from `controllers/heroes.py` through `crud/base.py`
and `repositories/sqlalchemy.py` to the database takes three files
instead of one. `import-linter`'s contract makes that indirection safe
to keep generic — a future contributor who reaches for a shortcut
(importing a model into `crud/base.py`, say) gets a failing build
instead of a slow drift back toward one-class-per-resource.

`CRUDInterface`/`Repository` cover the common create/read/update/delete
shape only. A resource that genuinely needs bespoke query logic (a
search endpoint, a bulk operation) adds that directly in its controller
against the repository or session, rather than forcing it through the
generic interface — `src/app/crud/README.md` says so explicitly, so
that pressure doesn't reintroduce resource-specific methods onto the
shared `CRUDInterface` class over time.

Registering a health check is a two-line addition
(`registry.register(...)` in `get_health_registry`, plus the concrete
`HealthCheck` class in `health/checks.py`), which was worth it given the
task explicitly called for a registry future services plug into — but
it is one more layer than a hand-written `/health` route that just
pings a fixed list of things, and that cost only pays for itself once a
second or third service is added.
