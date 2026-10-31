# FR-0018. Render every error as an RFC 9457 problem-details response

## Status

Implemented

## Description

The system shall render every `HTTPException`, request-validation
error, and otherwise-unhandled exception as a consistent
`application/problem+json` body (`type`, `title`, `status`, `detail`,
`instance`), replacing FastAPI's default `{"detail": ...}` shape.
Validation failures shall use status 422 with `detail` set to the list
of validation errors.

## Source

API consumers. Implemented in `src/app/problem_details.py`.

## Acceptance criteria

- Any error response, regardless of source (HTTP exception,
  validation, unhandled exception), has the same problem-details
  field shape and `application/problem+json` content type.
- A validation failure returns 422 with the specific field errors in
  `detail`.
