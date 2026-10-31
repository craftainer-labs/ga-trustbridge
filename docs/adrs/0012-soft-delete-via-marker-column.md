# 0012. Soft-delete Archivable resources via a marker column, not a second table

## Status

Accepted

## Context

`docs/plans/2026-09-add-draft-and-archive-functionality.md` added a set of
opt-in record-lifecycle behaviors to the generic CRUD framework (archive,
draft, scheduled publish/unpublish, scheduled purge, clone, lock,
revision history), demonstrated on `Hero`. Archive needed a storage
decision up front: `DELETE` should no longer remove a row outright, but
mark it archived, excludable/includable from normal reads, with a way to
restore it (single or bulk) — where does an archived row actually live?

Two shapes were considered:

1. **A second table per archivable resource** (`heroes` + `heroes_archive`,
   moved on delete/restore).
2. **A marker column on the same table** (`archived_at: datetime | None`),
   set instead of issuing a real `DELETE`.

The deciding constraint is `app.repositories`' own genericity:
`Repository[ModelT]`/`SQLAlchemyRepository`/`InMemoryRepository` are all
parameterized by a single SQLAlchemy model bound to a single table — every
generic method (`list`/`filter`/`sort`/`count`/the bulk actions) works for
any resource precisely because it never hardcodes a table. A second table
per archivable resource would need a matching implementation of every one
of those generic methods against that second table too (its own filtering,
sorting, pagination, bulk-action counting), duplicating the entire generic
repository layer for every future archivable resource — the opposite of
what `app.repositories/README.md` and `docs/adrs/0001-mvc-layering-with-a-
generic-crud-interface.md` set out to keep generic.

## Decision

**A marker column on the same row**, not a second table.
`app.models.mixins.Archivable` adds `archived_at: Mapped[datetime | None]`
(default `None`); a model opts in with plain multiple inheritance
(`class Hero(IdentifiedBase, Archivable, ...)`). `SQLAlchemyRepository`/
`InMemoryRepository` detect the column via `hasattr` (mirroring how
`app.repositories.memory` already special-cases `created_at`/`updated_at`)
and set it instead of issuing a real delete; `list`/`get`/`count` add a
default exclusion clause for it, overridable per-call via
`include_archived`. A model without the mixin is completely unaffected —
no shared base class changes, no new required column anywhere else.

Every existing generic mechanism — filtering, sorting, bulk actions,
pagination — keeps working against archived rows for free: "list archived
heroes created last week" is just `?include_archived=true&created_at__gte=...`
on the same `/crud/v1/heroes/v2/json` route, no new query surface, no
second repository implementation, no second `CRUDInterface`.

## Consequences

An archived row still occupies table and index space indefinitely (until
`app.maintenance.purge_archived` removes it, if a deployment enables
purge at all) and still collides with any unique constraint the table
carries — a re-`POST` with the same unique value as an archived row still
fails, since the row hasn't actually gone anywhere. A second-table design
would have avoided both, at the cost of duplicating the entire generic
repository/CRUD layer per archivable resource; that tradeoff was judged
not worth it for this template, where "one more generic mixin" is cheap
and "duplicate the generic layer per feature" compounds badly as more
mixins (draft, schedulable, lockable) are added on top of the same rows.
A future resource with unique constraints that must be reusable after
archiving (e.g. a username) needs its own handling for that (e.g.
suffixing the unique value on archive) — not something this decision
solves generically.
