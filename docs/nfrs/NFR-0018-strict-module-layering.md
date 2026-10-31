# NFR-0018. Enforce strict one-directional module layering

## Attribute

Maintainability / architecture.

## Description

Import order shall be enforced and CI-gated: `config → telemetry →
problem_details → oidc → models → views → repositories → crud →
health → web_components → xml_codec → http_headers → controllers →
main`. A lower layer shall never import from a higher one.

## Source

Developers maintaining the template. See
[0001-mvc-layering-with-a-generic-crud-interface](../adrs/0001-mvc-layering-with-a-generic-crud-interface.md);
documented in `src/app/README.md`'s "Layering" section.

## Verification

CI-gated via import-linter (or equivalent) as part of
`prek run --all-files --hook-stage manual`.
