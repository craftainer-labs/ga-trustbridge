# NFR-0005. Make adding a new deprecated version a fixed, low-cost pattern

## Attribute

Maintainability.

## Description

Introducing a new deprecated API version shall require only a views
module plus a thin controller mirroring the current version, named
`*_vN.py`, with no new persistence or CRUD infrastructure.

## Source

Developers maintaining the template. See
[0002-api-and-model-versioning](../adrs/0002-api-and-model-versioning.md),
`src/app/views/README.md`.

## Verification

Code review against the documented naming convention; demonstrated by
`heroes_v1.py`/`hero_v1.py` following the same pattern the next
version would use.
