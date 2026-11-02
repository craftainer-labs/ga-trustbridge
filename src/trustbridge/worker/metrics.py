"""OpenTelemetry instruments (no-ops until a MeterProvider is configured, as in the template)."""

from collections.abc import Iterable

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation

_meter = metrics.get_meter("trustbridge")

limiter_failopen = _meter.create_counter(
    "trustbridge.webhooks.ratelimit.failopen",
    description="Webhook requests let through because the Valkey limiter errored or timed out",
)
limiter_rejected = _meter.create_counter(
    "trustbridge.webhooks.ratelimit.rejected",
    description="Webhook requests rejected with 429",
)
deliveries_dead = _meter.create_counter(
    "trustbridge.deliveries.dead", description="Deliveries moved to the dead letter"
)
deliveries_failed = _meter.create_counter(
    "trustbridge.deliveries.failed", description="Handler failures (the failure counter)"
)

_lag: dict[str, int] = {}


def set_stream_lag(lag: int | None) -> None:
    """Record the latest `tb:events` group lag for the observable gauge."""
    if lag is None:
        _lag.pop("lag", None)
    else:
        _lag["lag"] = lag


def _observe_lag(_options: CallbackOptions) -> Iterable[Observation]:
    """Gauge callback."""
    if "lag" in _lag:
        yield Observation(_lag["lag"])


_meter.create_observable_gauge(
    "trustbridge.stream.lag",
    callbacks=[_observe_lag],
    description="Entries of tb:events not yet delivered to the tb-workers group",
)
