# FR-0003. Validate Hero fields on write

## Status

Implemented

## Description

The system shall reject a Hero create/update whose `name` is not
1-200 characters, or whose `powers` is not a list of at least one
string each 1-200 characters.

## Source

API consumers. Implemented in `src/app/views/hero.py`.

## Acceptance criteria

- Submitting a hero with an empty `name`, a `name` over 200
  characters, an empty `powers` list, or a power string outside
  1-200 characters is rejected with a validation error (see
  [FR-0018](FR-0018-uniform-problem-details-errors.md)).
