"""The webhook abuse limiter (ADR 0028 "Abuse limits on the webhook route"; invariants 25-28).

The counter and the fail-open core live in `trustbridge.ratelimit` (shared with the admin API,
ADR 0033 decision 5); this module binds them to the webhook route's key prefix and metrics. It
is the first step of the webhook route, before the `Content-Length` check, any body read and
the signature check, so bad-signature requests count. It reads only `request.client.host` (the
ASGI client address, which uvicorn's `ProxyHeadersMiddleware` rewrites for `TRUSTED_PROXIES`)
and never parses `X-Forwarded-For`. The whole check, connect included, is bounded by
`WEBHOOK_RATE_LIMIT_TIMEOUT_MS`.
"""

from dataclasses import dataclass

from trustbridge.ratelimit import (
    IPV6_PREFIX,
    UNKNOWN_ADDRESS,
    CounterLike,
    FixedWindowLimiter,
    InMemoryWindowCounter,
    LimitDecision,
    ValkeyWindowCounter,
    WindowCounter,
    source_key,
)
from trustbridge.worker import metrics

KEY_PREFIX = "tb:rl:webhooks"


@dataclass(slots=True)
class WebhookLimiter(FixedWindowLimiter):
    """The per-address fixed-window limiter of the webhook route."""

    prefix: str = KEY_PREFIX
    rejected: CounterLike | None = metrics.limiter_rejected
    failopen: CounterLike | None = metrics.limiter_failopen


__all__ = [
    "IPV6_PREFIX",
    "KEY_PREFIX",
    "UNKNOWN_ADDRESS",
    "InMemoryWindowCounter",
    "LimitDecision",
    "ValkeyWindowCounter",
    "WebhookLimiter",
    "WindowCounter",
    "source_key",
]
