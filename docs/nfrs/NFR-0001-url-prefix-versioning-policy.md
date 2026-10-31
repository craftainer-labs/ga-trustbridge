# NFR-0001. Version every API representation via an explicit URL prefix

## Attribute

Compatibility / versioning policy.

## Description

Every resource shall be mounted under an explicit `/vN` prefix; no
unversioned bare alias shall exist, so callers must always pick a
version explicitly.

## Source

API consumers; developers maintaining the template. See
[0002-api-and-model-versioning](../adrs/0002-api-and-model-versioning.md).

## Verification

Reviewed at PR time: any new route must be mounted under a versioned
prefix; covered incidentally by existing route tests asserting `/v1`
and `/v2` paths.
