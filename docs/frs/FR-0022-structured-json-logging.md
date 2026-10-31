# FR-0022. Emit all logs as structured JSON

## Status

Implemented

## Description

The system shall emit every log line — including uvicorn's own — as
one structured JSON object to stdout, with `timestamp`, `level`,
`logger`, `message`, arbitrary extras, and exception info when
present.

## Source

Operators/SRE. Implemented in `craftainer_observability.logs` (the craftainer-observability package).

## Acceptance criteria

- Every line written to stdout by the running app is valid, single-
  object JSON with at least `timestamp`, `level`, `logger`, and
  `message` keys.
