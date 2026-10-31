# 0008. Put schema-driven filter/sort/bulk-action logic in the shared CRUD interface, not per-resource controllers

## Status

Accepted

## Context

`app/controllers/crud_router.py`'s generated routes supported only
`skip`/`limit` pagination and path-addressed single-record get/update/
delete. Adding per-field filtering, sorting, and single/bulk update/
delete meant deciding where that logic lives.

[0001](0001-mvc-layering-with-a-generic-crud-interface.md)'s
"Consequences" section says explicitly: "A resource that genuinely needs
bespoke query logic (a search endpoint, a bulk operation) adds that
directly in its controller against the repository or session, rather
than forcing it through the generic interface." That guidance was
written with a *resource-specific* search/bulk endpoint in mind — one
whose shape reflects business rules only that resource has. The filter/
sort/bulk capability actually being added here is different in kind: it
is derived mechanically from a resource's own Pydantic schema (a numeric
field always gets range/membership operators, a string field always
gets substring operators, and so on), so a resource-by-resource
implementation would mean re-deriving the identical logic in every
controller from that controller's own view classes — exactly the
per-resource duplication [0001](0001-mvc-layering-with-a-generic-crud-interface.md)
was written to avoid in the first place.

## Decision

We will put the generic, schema-driven parts of this capability in the
same shared layers `CRUDInterface`/`Repository` already live in, not in
`heroes.py` or any other resource's controller:

- `app/repositories/filtering.py`'s `FilterClause`/`SortClause`/`FilterOp`
  are storage-agnostic value objects, interpreted by each concrete
  `Repository` implementation (`SQLAlchemyRepository`/`InMemoryRepository`)
  the same way the existing CRUD operations are.
- `Repository`/`CRUDInterface`/`CompatCRUD` gain `filters`/`sort`
  parameters on `list`, plus `count`/`update_many`/`delete_many` — pure
  passthrough, no resource-specific code, matching every other method
  already on those classes.
- `app/controllers/crud_query.py` turns an HTTP query string into
  `FilterClause`/`SortClause` sequences by introspecting a resource's own
  Pydantic schema (numeric fields get `eq`/`min`/`max`/`in`, string
  fields get `eq`/`contains`/`icontains`/`regex`, bool/Enum/Literal
  fields get `eq`/`in`) — one implementation, reused by every resource's
  routes, rather than each controller writing its own query-parsing.
- `app/controllers/crud_actions.py`'s `resolve_list_or_get`/
  `resolve_update`/`resolve_delete` implement the "id present → single
  record; otherwise → filtered list, or a bulk action over the given
  filters" decision once, shared by `build_json_router`/
  `build_xml_router`.

A resource controller still writes its own logic for anything that
isn't mechanically derivable from its schema — a business-rule search
endpoint, a multi-step workflow, an operation scoped to something other
than a filter set. That case keeps following
[0001](0001-mvc-layering-with-a-generic-crud-interface.md)'s original
guidance unchanged.

## Consequences

Every resource built on `build_json_router`/`build_xml_router` gets
filtering, sorting, single/bulk update, and bulk delete for free, the
same way it already gets create/read/update/delete for free — no
resource-specific query code to write or keep in sync across resources.
`app/controllers/heroes.py` did not change at all to gain this
capability; only the shared factories and the layers below them did.

The cost mirrors [0001](0001-mvc-layering-with-a-generic-crud-interface.md)'s
original one: `CRUDInterface`/`Repository` now carry more surface area
(`count`/`update_many`/`delete_many`, `filters`/`sort` on `list`), and a
future contributor reaching for "just add a search endpoint" needs to
recognize which case they're in — mechanically-derivable-from-the-schema
(belongs here) versus genuinely resource-specific (belongs in the
controller, per [0001](0001-mvc-layering-with-a-generic-crud-interface.md)).
`app/crud/README.md`'s "Don't add a resource-specific method to
CRUDInterface" guidance is unchanged and still the right call for that
second case — this ADR narrows the first paragraph of
[0001](0001-mvc-layering-with-a-generic-crud-interface.md)'s bulk/search
guidance, not the whole document.

`count`/`CompatCRUD.count`/`SQLAlchemyRepository.count` are added but not
yet called by any route (reserved for a future total-count response
header), so that method is currently reachable only from
`tests/unit`/`tests/integration`, not through the live HTTP stack
`tests/e2e` exercises — a deliberate, documented gap (see
`app/repositories/sqlalchemy.py`'s `count` docstring), not an oversight.
