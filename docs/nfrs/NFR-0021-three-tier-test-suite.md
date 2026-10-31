# NFR-0021. Maintain three independent test tiers

## Attribute

Quality / process.

## Description

Tests shall be split into `tests/unit` (against `TestClient(app)` with
fakes, no lifespan), `tests/integration` (real backing services for
select paths), and `tests/e2e` (Playwright, driving one live
`MODE=dev` process end-to-end).

## Source

Developers; QA/CI. Documented in `tests/README.md`.

## Verification

CI runs all three tiers as distinct steps/commands; each tier's
existence and scope is reviewed against `tests/README.md`.
