# NFR-0020. Enforce a 95% automated test coverage gate

## Attribute

Quality / process.

## Description

`pytest` shall fail below 95% coverage of `src/app`, for both the
default run (`tests/unit` + `tests/integration`) and
`uv run pytest tests/e2e` independently.

## Source

Developers; QA/CI. Configured in `pyproject.toml`; documented in
`docs/TEMPLATE.md`'s "Checks" section.

## Verification

CI runs `prek run --all-files --hook-stage manual`, which
fails the build if either coverage run drops below 95%.
