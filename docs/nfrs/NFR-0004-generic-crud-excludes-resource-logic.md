# NFR-0004. Keep resource-specific logic out of the generic CRUD interface

## Attribute

Maintainability.

## Description

`CRUDInterface` shall not grow resource-specific methods; bespoke
query or bulk-operation logic belongs directly in the owning
controller, not in the shared interface.

## Source

Developers maintaining the template. Documented in
`src/app/crud/README.md`.

## Verification

Code review against `src/app/crud/README.md`'s "Don't" section; no
automated check.
