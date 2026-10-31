# FR-0009. Provide a zero-JS HTML form with progressive enhancement

## Status

Implemented

## Description

The system shall serve `/heroes/form` as a plain HTML `<form>` page
that fully works with JavaScript disabled, plus
`/heroes/components.js`, vanilla-JS custom elements that
progressively enhance the same page against the existing JSON CRUD
endpoints. A list-valued field (e.g. `powers`) shall be submitted and
rendered as a comma-separated string. A submission that fails
validation shall respond with 422 (see
[FR-0018](FR-0018-uniform-problem-details-errors.md)); a successful
submission shall respond `303 See Other` back to the form page.

## Source

API consumers / browser users. Implemented in
`src/app/web_components.py`, `src/app/controllers/heroes_web.py`.

## Acceptance criteria

- Submitting the form with JavaScript disabled creates a hero and
  redirects back to the form.
- Submitting the form with a missing required field returns 422
  rather than a 500 or a silent failure.
- A comma-separated `powers` field round-trips correctly to a list.
