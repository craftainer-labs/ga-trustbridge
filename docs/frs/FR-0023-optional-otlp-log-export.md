# FR-0023. Optionally export logs via OTLP

## Status

Implemented

## Description

The system shall, when `OTEL_EXPORTER_OTLP_ENDPOINT` is set,
additionally bridge logs to an OTLP log exporter via a batch
processor, relying on OpenTelemetry's own environment-variable
conventions rather than re-modeling them as app settings.

## Source

Operators/SRE. Implemented in `craftainer_observability.logs` (the craftainer-observability package).

## Acceptance criteria

- With `OTEL_EXPORTER_OTLP_ENDPOINT` unset, no OTLP export is
  attempted and stdout logging is unaffected.
- With it set, logs are additionally delivered to the configured OTLP
  endpoint.
