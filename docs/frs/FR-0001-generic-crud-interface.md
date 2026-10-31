# FR-0001. Provide a generic, resource-agnostic CRUD interface

## Status

Implemented

## Description

The system shall expose list/create/get/update/delete operations
through a generic `CRUDInterface` parameterized by a Pydantic view and
a storage-agnostic `Repository`, so that adding a new resource
requires no new CRUD logic — only a model, a view, and a repository.

## Source

Developers maintaining the template. See
[0001-mvc-layering-with-a-generic-crud-interface](../adrs/0001-mvc-layering-with-a-generic-crud-interface.md).
Implemented in `src/app/crud/base.py`.

## Acceptance criteria

- A new resource can be wired into list/create/get/update/delete
  routes by supplying a model, view, and repository, with zero
  resource-specific CRUD code.
- Adding CRUD support for a resource does not require modifying
  `CRUDInterface` itself.
