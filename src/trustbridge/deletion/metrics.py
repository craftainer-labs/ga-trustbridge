"""The deletion engine's instruments (no-ops until a MeterProvider is configured)."""

from opentelemetry import metrics

_meter = metrics.get_meter("trustbridge")

attempts = _meter.create_counter(
    "trustbridge.deletion.attempts",
    description="Transient step failures of tenant deletions (each is retried with backoff)",
)
failed = _meter.create_counter(
    "trustbridge.deletion.failed",
    description="Tenant deletions written failed for a non-retryable error",
)
