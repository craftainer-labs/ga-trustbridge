"""OpenTelemetry instruments of the admin API (no-ops until a MeterProvider is configured)."""

from opentelemetry import metrics

_meter = metrics.get_meter("trustbridge")

rate_limit_failopen = _meter.create_counter(
    "trustbridge.admin.ratelimit.failopen",
    description="Admin requests let through because the Valkey limiter errored or timed out",
)
rate_limit_rejected = _meter.create_counter(
    "trustbridge.admin.ratelimit.rejected",
    description="Admin requests rejected with 429 by a per-principal bucket",
)
rejected_limited = _meter.create_counter(
    "trustbridge.admin.ratelimit.anonymous_rejected",
    description="Rejected (401, 403, 404) admin requests answered 429 by the per-address bucket",
)
authz_denied_suppressed = _meter.create_counter(
    "trustbridge.audit.authz_denied.suppressed",
    description="authz.denied events counted but not recorded (over the per-subject burst)",
)
