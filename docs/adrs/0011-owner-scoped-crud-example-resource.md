# 0011. Add an opt-in `OwnerScope` hook to `CRUDInterface`, demonstrated on Hero

## Status

Accepted

## Context

`docs/adrs/0008-generic-schema-driven-query-and-bulk-actions.md`'s
generic CRUD/repository layer had no per-user (row-level) scoping hook:
any authenticated caller with the right role could list, filter, or
bulk-update/delete any record of a resource, regardless of who created
it. Not a problem for Hero as it stood (nothing in this app was "owned"
by a specific user), but a future resource with per-user data (a user's
own draft posts, a tenant's own records) would silently inherit this
gap — nothing in `app.repositories`/`app.interfaces` stopped a
broader-than-intended query, and there was no established pattern to
reach for.

This was deliberately deferred out of `docs/plans/
2026-09-owasp-top-ten-hardening.md`'s A01 findings and tracked in
`docs/plans/2026-09-repository-ownership-scoping.md`, since building it
speculatively — with no real resource to validate the shape against —
risked guessing wrong. That plan was later executed speculatively
anyway, at the user's explicit request, once the decisions below had
been made; this ADR is the resulting decision record, and the plan
document is folded into it and removed per `docs/plans/README.md`.

Two design questions had to be resolved, in order:

1. **Where does the hook live?** `Repository.get`/`update`/`delete`
   take only a record id, no filters at all — scoping those by owner
   means either adding filter parameters to every `Repository`
   implementation's single-record methods, or reusing the
   `list`/`update_many`/`delete_many` methods those implementations
   already have.
2. **Which existing resource demonstrates it?** The first attempt added
   a brand-new `Note` resource rather than touching Hero, to avoid
   changing Hero's existing behavior. That turned out to be the wrong
   call: Hero is the app's one worked example, referenced from
   `app/README.md`, `crud_1/README.md`, and most of `tests/`, and a
   second, purpose-built example resource whose only job was
   demonstrating a single feature added maintenance surface (a model, a
   migration, a router, three test tiers) without adding anything a
   reader couldn't already see on Hero. Once Hero itself was confirmed
   as the right place, a second problem surfaced: several existing e2e
   journeys (`tests/e2e/viewer`, `editor`, `detective`) treat Hero as a
   *shared* resource — a `maintainer`-role user seeds a hero that a
   `viewer`/`detective`-role user then reads. Fully scoping reads to the
   creator would break every one of those cross-role reads.

## Decision

**Injection point**: `CRUDInterface`, not `Repository`. `app.interfaces.
base.OwnerScope(field, value, read_scoped=True)` is a frozen dataclass;
`CRUDInterface(..., owner=OwnerScope(...))` is opt-in per resource —
`owner=None` (the default) changes nothing. `update`/`delete` (which
take only a record id) route through `update_many`/`delete_many` with
an added `id`-equality filter when `owner` is set, instead of calling
`Repository.update`/`delete` directly — this needed no change to
`Repository`'s protocol or either concrete implementation
(`SQLAlchemyRepository`/`InMemoryRepository`), so `app.repositories`
stays unaware "ownership" exists as a concept at all, per
`app.repositories/README.md`'s "Don't give `SQLAlchemyRepository`
resource-specific logic". `create` stamps the owner field from the
scope's `value` rather than trusting it from client input.

**`read_scoped`**: `list`/`get`/`count` are owner-restricted too by
default (`read_scoped=True`), matching `update`/`delete`. Passing
`read_scoped=False` opens those three to every caller while
`update`/`delete`/`update_many`/`delete_many` stay owner-restricted
regardless — every authenticated caller sees every record, but can only
write their own. This is what let the feature land on Hero without
rewriting its shared-read e2e journeys: `app.crud_1.heroes.heroes_v2.
get_hero_crud` passes `OwnerScope("owner_id", claims["sub"],
read_scoped=False)`, so `viewer`/`detective` still read a hero
`maintainer` created, but only `maintainer` (specifically, the same
`maintainer` login that created it) can now update or delete it.

**Demonstrated on Hero directly**, not a separate resource: `Hero`
gained an `owner_id` column (migration `4733a4dd6da9`, which also wipes
the table's pre-existing rows — see that migration's own comment for
why that's safe here specifically), `HeroV2` gained a read-only
`owner_id` field (never accepted by `HeroV2Create`/`HeroV2Update`), and
`get_hero_crud` passes the `OwnerScope` above. The deprecated
`/crud/v1/heroes/v1` sibling inherits this for free, since
`CompatCRUD` wraps the same (now owner-scoped) `CRUDInterface`.

The owner `value` is resolved from the caller's own claims
(`claims["sub"]`), read the same way any other per-request value
reaches a `get_<resource>_crud` builder — as a `Depends
(get_current_claims)` parameter — not a new mechanism.

## Consequences

Any future resource with per-user or per-tenant data (draft posts, a
tenant's own records) has a one-line opt-in —
`owner=OwnerScope(field, claims["sub"])`, with or without
`read_scoped=False` depending on whether reads should stay shared — in
its own `get_<resource>_crud`, instead of needing to invent row-level
scoping from scratch or, worse, shipping without it.
`tests/unit/interfaces/test_base.py` covers the hook generically (two
scoped `CRUDInterface`s sharing one fake repository, both the
fully-scoped and `read_scoped=False` shapes); `tests/unit/crud_1/
heroes/test_heroes_v2.py` and `tests/integration/crud_1/heroes/
test_heroes_v2.py` cover it end-to-end over the real HTTP routes (the
latter against real Postgres), proving a second caller can read but not
update/delete/bulk-update/bulk-delete another owner's hero.

The cost: `CRUDInterface` now has a second constructor parameter and an
internal branch in every one of its methods except `create`, and a
resource opting into the fully-scoped shape gets a subtly different
execution path for `get`/`update`/`delete` (routed through
`list`/`update_many`/`delete_many` with an extra filter, rather than
the repository's own single-record methods) — a future reader debugging
an owned resource's 404 needs to know this rewrite happens. Every Hero
row created before this migration is gone (the migration deletes them
rather than backfilling a fabricated owner), which is fine for this
template's own disposable example data but is exactly the kind of
migration a forked app must never copy verbatim against real data.
One existing e2e journey (`tests/e2e/editor/test_editor_journey.py`)
lost the ability to actually clean up the heroes it creates — editor
lacks the maintainer-only delete role, and maintainer is no longer the
owner of a hero editor created, so neither can delete it anymore; that
cleanup call now passes `fail_on_status_code=False` and is documented
as a known, accepted gap (test data accumulates in the dev-mode e2e
Postgres across runs) rather than something this ADR's scope fixes.
