"""Redaction for `before` and `after` (ADR 0032 "Redaction").

Events are built from an **allow-list per resource shape**: `project` copies only the named
fields of a source mapping, so a new column is absent from the audit until someone adds it here.
Values are scalars only (nested data is refused) and every string goes through `scrub_text`
(control characters dropped, secret shapes masked, length bounded). An unknown shape is an error
(fail closed), never an unfiltered dump.
"""

import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

MAX_TEXT = 256
"""Longest stored string value; a longer one is cut and marked."""
TRUNCATION_MARKER = "...[truncated]"
REDACTED = "[REDACTED]"

# Credential-shaped `key=value` / `key: value` pairs and long opaque runs, masked in free text.
_SECRET_PAIR = re.compile(
    r"(?i)(token|password|passwd|secret|authorization|credential|api[_-]?key|private[_-]?key)"
    r"[\"']?(\s*[=:]\s*|\s+)[\"']?(?:(?:bearer|basic)\s+)?\S+"
)
_OPAQUE_RUN = re.compile(r"[A-Za-z0-9+/_=-]{40,}")
SECRET_WORDS = ("token", "password", "passwd", "secret", "authorization", "credential", "key")
"""Substrings that mark a mapping key as secret-bearing; a read drops such a key (fail closed)."""


class FailureReason(StrEnum):
    """Why a mutating admin call failed: a small fixed set, never text from an exception.

    A `failure` event records one of these in `after` (the `failure` shape), so an auditor can
    tell a refusal from an outage without the event carrying anything a caller or an upstream
    wrote. Add a member here (and to ADR 0032's list) rather than recording a message.
    """

    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    TENANT_DISABLED = "tenant_disabled"
    NOT_ROTATABLE = "not_rotatable"
    NOT_CONFIGURED = "not_configured"
    LEASE_BUSY = "lease_busy"
    LEASE_LOST = "lease_lost"
    INVALID_STATE = "invalid_state"
    INVALID_KEY = "invalid_key"
    HOST_NOT_READY = "host_not_ready"
    STORE_UNAVAILABLE = "secret_unavailable"  # the secret store
    UPSTREAM = "upstream"
    UNAVAILABLE = "unavailable"
    CANCELLED = "cancelled"
    INTERNAL = "internal"

    @classmethod
    def for_status(cls, status_code: int) -> FailureReason:
        """The reason a refusal with this HTTP status implies: 404, 409, else `unavailable`."""
        return {404: cls.NOT_FOUND, 409: cls.CONFLICT}.get(status_code, cls.UNAVAILABLE)


ALLOW_LISTS: Mapping[str, frozenset[str]] = {
    "tenant": frozenset({"state"}),
    "credential": frozenset(
        {
            "adapter",
            "kind",
            "operation_id",
            "operation_kind",
            "generation",
            "delivered",
            "previous_generation",
            "retire_after",
        }
    ),
    "oidc_trust": frozenset({"adapter", "operation_id"}),
    # NG6e: the admin-disable columns of a tenant (ADR 0033 "Tenant disable"), before and after.
    "tenant_admin": frozenset({"admin_disabled_at", "admin_disabled_by", "admin_disable_reason"}),
    # NG6e: a webhook delivery's retry state (requeue). The headers and the body never appear.
    "delivery": frozenset({"status", "attempts", "next_attempt_at", "dead_at", "last_error"}),
    # Connection data is recorded as `{"configured": true}` and nothing else (ADR 0032). No
    # admin route writes it; the shape exists so any future writer cannot record a value.
    "connection": frozenset({"configured"}),
    # NG6e-4: a host registration (ids and the secret's reference, never the key or the secret).
    "host_registration": frozenset({"kind", "app_id", "app_slug", "webhook_secret_ref"}),
    # NG6e-6: the reason code of a failure event (`FailureReason`, never free text).
    "failure": frozenset({"reason"}),
    # NG6h-4a-2: a tenant deletion's step, skipped category, completion or failure (ADR 0034).
    # Ids, state names, the step name, a category, a count and a fixed code; never row content
    # or the organisation login. The per-category counts go through `deletion_counts`.
    "deletion": frozenset(
        {
            "deletion_id",
            "step",
            "from_state",
            "to_state",
            "category",
            "reason",
            "count",
            "attempts",
            "error_code",
        }
    ),
    "authz_denial": frozenset(
        {"reason", "required_role", "scope", "suppressed", "window_seconds", "window", "sample"}
    ),
    "audit_export": frozenset(
        {
            "scope",
            "tenant_id",
            "after_seq",
            "since",
            "until",
            "actor_sub",
            "action",
            "resource_type",
            "resource_id",
            "outcome",
            "row_count",
            "last_seq",
            "complete",
        }
    ),
}
"""The fields each shape may record. Secrets and connection data never appear here."""


def truncate(value: str, limit: int = MAX_TEXT) -> str:
    """`value` cut to `limit` characters including the marker, which says it was cut."""
    if len(value) <= limit:
        return value
    return value[: limit - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def printable(value: str) -> str:
    """`value` with every non-printable character replaced by a space (no masking, no cut)."""
    return "".join(c if c.isprintable() else " " for c in value)


def has_secret_word(key: str) -> bool:
    """True when `key` contains a secret word (`SECRET_WORDS`), compared case-insensitively."""
    lowered = key.lower()
    return any(word in lowered for word in SECRET_WORDS)


def scrub_text(value: str, limit: int = MAX_TEXT) -> str:
    """Free text made safe to store: control characters dropped, secret-shaped parts masked.

    A PEM block is replaced whole; `token=...`-style pairs and long opaque runs are masked; the
    result is bounded by `truncate`. This is the same policy the logs apply (a denylist of
    credential-shaped keys, ADR 0007), so an error message is recorded no more openly than logged.
    """
    if "-----BEGIN" in value:
        return REDACTED
    masked = _SECRET_PAIR.sub(lambda m: f"{m.group(1)}={REDACTED}", printable(value))
    return truncate(_OPAQUE_RUN.sub(REDACTED, masked), limit)


def _scalar(key: str, value: Any) -> Any:  # noqa: ANN401 -- a JSON scalar of any kind
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{key} is not a finite number")
        return value
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    if isinstance(value, UUID):
        return str(value)
    raise ValueError(f"{key} has a value of a type that is not recorded")


MAX_COUNT_KEY = 64
"""The longest category name `deletion_counts` accepts."""


def project(shape: str, source: Mapping[str, Any]) -> dict[str, Any]:
    """The allow-listed fields of `source` for `shape`, as JSON scalars, in sorted key order.

    Fields not on the list are dropped silently (that is the point); a field on the list but
    absent from `source` is simply not recorded. Raises `ValueError` for an unknown shape or a
    value that is not a scalar, so nothing unreviewed is stored.
    """
    allowed = ALLOW_LISTS.get(shape)
    if allowed is None:
        raise ValueError(f"no audit allow-list for {shape!r}")
    return {key: _scalar(key, source[key]) for key in sorted(allowed) if key in source}


def failure_after(reason: FailureReason) -> dict[str, Any]:
    """The `after` of a `failure` event: the reason code and nothing else.

    Takes a `FailureReason` only, so a message from an exception cannot get in by this route.
    """
    if not isinstance(reason, FailureReason):
        raise ValueError("a failure reason must be a FailureReason")
    return project("failure", {"reason": reason.value})


def deletion_counts(counts: Mapping[str, Any]) -> dict[str, int]:
    """`counts` (rows or resources removed per category) as a `{category: count}` object.

    A deletion's counts are the one non-scalar value a shape records, so they have their own
    check: printable category names within 64 characters and non-negative integers (no bool),
    in sorted order. Anything else raises `ValueError`, so nothing unreviewed is stored.
    """
    out: dict[str, int] = {}
    for key in sorted(counts):
        value = counts[key]
        if (
            not isinstance(key, str)
            or not key
            or len(key) > MAX_COUNT_KEY
            or printable(key) != key
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
        ):
            raise ValueError("deletion counts must map a short category name to a count")
        out[key] = value
    return out
