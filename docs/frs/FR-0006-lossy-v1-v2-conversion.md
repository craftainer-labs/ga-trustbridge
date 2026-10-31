# FR-0006. Convert between Hero v1 and v2 shapes predictably

## Status

Implemented

## Description

The system shall convert v2→v1 by taking `powers[0]` as
`superpower` (documented as lossy), v1→v2 create by wrapping
`superpower` into a single-element `powers` list, and v1→v2 update by
mapping `superpower` to `powers` only when `superpower` was actually
supplied — never clobbering existing `powers` when it was omitted.

## Source

Legacy API consumers; developers maintaining the template. Implemented
in `src/app/views/hero_v1.py`.

## Acceptance criteria

- A `PATCH` to a v1 hero that omits `superpower` leaves the
  underlying `powers` list unset/unchanged.
- A `PATCH` to a v1 hero that includes `superpower` sets `powers` to
  a single-element list containing it.
- Reading a hero with multiple powers through v1 returns only the
  first power as `superpower`.
