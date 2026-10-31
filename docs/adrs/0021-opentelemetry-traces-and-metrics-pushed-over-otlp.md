# 0021. Add OpenTelemetry traces and metrics, pushed over OTLP, behind an optional extra

## Status

Accepted

## Context

[`0007`](0007-structured-json-logging-only.md) limited observability to
structured logs with optional OTLP log export, and said tracing or
metrics should arrive as a new ADR. The app is being prepared for
Kubernetes, where operators need request-duration histograms, query
spans and connection-pool metrics, and a way to follow a slow trace from
its logs and back. The pooling change (`DB_POOL_MODE`, superseding the
`NullPool` trade-off of `NFR-0016`) can only be verified in production
with pool metrics.

A scraped Prometheus `/metrics` endpoint has a multi-worker problem
(`prometheus_client` multiprocess mode), and OpenTelemetry's Python
documentation recommends OTLP push for forking servers. Prometheus 3
accepts OTLP over HTTP (`--web.enable-otlp-receiver`, at
`/api/v1/otlp/v1/metrics`), and an OTel Collector accepts it too.
Kubernetes deployments run one worker per pod and scale by adding pods.

## Decision

This ADR supersedes `0007`'s decision not to add tracing or metrics
(see also [`NFR-0025`](../nfrs/NFR-0025-observability-scope-logs-traces-and-metrics.md)).
`0007`'s other rule stands: OpenTelemetry's standard environment
variables are the only configuration and are never re-modelled on
`Settings`.

We will push all three signals over OTLP and add no `/metrics` endpoint.
A scrape endpoint is added only if a cluster has neither a collector nor
an OTLP receiver.

- **Packaging.** Providers and instrumentation live in
  `craftainer_tool_observability.otel`
  ([`0020`](0020-extract-crud-and-observability-into-published-packages.md)),
  installed through the `[otel]` extra (with `[sqlalchemy]` for the
  engine parts). `app.main` only calls `instrument_app()` and
  `instrument_engine()`.
- **Switch.** Same as today: nothing runs unless
  `OTEL_EXPORTER_OTLP_ENDPOINT` is set, with no behaviour change when it
  is unset. Per-signal control uses `OTEL_TRACES_EXPORTER`,
  `OTEL_METRICS_EXPORTER` and `OTEL_LOGS_EXPORTER` (`otlp` or `none`);
  `OTEL_SDK_DISABLED` is a full kill switch; sampling uses
  `OTEL_TRACES_SAMPLER` (+ `_ARG`); the metric interval uses
  `OTEL_METRIC_EXPORT_INTERVAL`; `OTEL_PYTHON_FASTAPI_EXCLUDED_URLS`
  excludes `/health/.*` so probes do not flood traces.
- **Providers.** A `TracerProvider` (batch span processor, OTLP/HTTP
  exporter) and a `MeterProvider` (`PeriodicExportingMetricReader`, OTLP/
  HTTP exporter) next to the existing `LoggerProvider`, sharing one
  `Resource`: `service.name` from `OTEL_SERVICE_NAME` (no settings
  dependency), and `service.version` from the `APP_VERSION` environment
  variable unless `OTEL_RESOURCE_ATTRIBUTES` already sets it (so canary
  and stable versions are distinguishable). `APP_COMMIT` is available to
  `/health/info`.
- **Instrumentation, OTel packages only.**
  `opentelemetry-instrumentation-fastapi` (server spans, per-route
  duration histograms) and `opentelemetry-instrumentation-sqlalchemy`
  attached to `engine.sync_engine` (query spans), plus observable gauges
  for pool state (checked out, overflow) if the instrumentation does not
  emit them. Versions follow the existing
  `opentelemetry-instrumentation-logging` release line.
- **Correlation.** Every JSON log line carries `trace_id` and `span_id`
  when a span is active. The request-ID middleware sets the span
  attribute `http.request.header.x-request-id`, so an ID from a client or
  gateway finds the trace.
- **Tests.** Unit tests use OTel's in-memory span and metric exporters
  (spans for a request and a query, health routes excluded, trace IDs in
  log lines). No collector is added to the devcontainer or `compose.yml`.
- **Overhead budget.** At the 1 vCPU / 2 GB reference budget
  ([`0019`](0019-per-pr-benchmarks-and-fixed-load-acceptance.md)),
  capacity and fixed-load runs are taken three ways: telemetry unset,
  sampling 100%, sampling 10%. Measured cost (fixed load, 25 users, 300 s,
  exact cgroup CPU, three runs, medians, `nop` exporter against a
  perf-only collector; baseline for this table is the final pre-OTel
  state at 7.244 ms and 69 ms p99):

  | Setting | CPU ms/request | p99 | Capacity |
  | --- | --- | --- | --- |
  | Telemetry unset | 7.244 | 69 ms | 130 users |
  | Sampling 100% | 8.069 (+11.4%) | 74 ms (+7.2%, beyond the spread) | 100 users (-23%) |
  | Sampling 10% | 7.789 (+7.5%) | 71 ms (within the spread) | 130 users (unchanged) |

  Most of the 10% cost is the OTLP log bridge and metrics export, which
  stay on at any sampling ratio whenever `OTEL_EXPORTER_OTLP_ENDPOINT` is
  set: about 9.8k log records and about 100 metric points per six-minute
  run at both ratios. If the cost matters, `OTEL_LOGS_EXPORTER=none` is
  the lever; it was not A/B'd separately. **Recommended default sampling:
  `OTEL_TRACES_SAMPLER=parentbased_traceidratio` with
  `OTEL_TRACES_SAMPLER_ARG=0.1`**, which keeps capacity unchanged and
  p99 within the spread. 100% sampling is for debugging windows.

## Consequences

Easier: slow requests can be traced to a query and to log lines; pool
behaviour is observable in production; any OTLP backend works without
app changes; the multi-worker scrape problem does not exist.

Harder: more dependencies (behind an opt-in extra) and SDK setup to
maintain; telemetry costs CPU, which the overhead measurement must bound,
since telemetry that eats the capacity it watches is itself a regression.
Operators need a collector or an OTLP receiver, because Prometheus cannot
scrape the app directly. The instrumentation packages are on OTel's
beta/semantic-convention schedule, so upgrades must track the pinned
release line.
