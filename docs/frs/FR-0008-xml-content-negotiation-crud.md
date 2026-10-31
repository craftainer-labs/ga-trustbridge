# FR-0008. Support XML as an alternative CRUD representation

## Status

Implemented

## Description

The system shall expose `/heroes/xml` (and `/v1/heroes/xml`)
supporting the same five CRUD operations as JSON, serializing and
deserializing `application/xml` bodies for flat resource models.

## Source

API consumers needing XML integration. Implemented in
`src/app/xml_codec.py`, `src/app/controllers/heroes_xml.py`.

## Acceptance criteria

- Each of list/create/get/update/delete works via XML request and
  response bodies with the same semantics as the JSON equivalents.
- A model with nested (non-flat) fields is not required to be
  supported by the XML codec (see
  [NFR-0006](../nfrs/NFR-0006-xml-codec-flat-models-only.md)).
