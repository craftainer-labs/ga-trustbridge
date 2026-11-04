"""The audit event as written, as read back, and the query over it (ADR 0032 "Event model")."""

import ipaddress
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from trustbridge.audit.actions import Action, SystemActor

SCHEMA_VERSION = 1
MAX_PAGE_SIZE = 1000
MAX_PAYLOAD_BYTES = 16384
"""Bound on the serialized `before` and on the serialized `after`."""
TEXT_LIMITS: dict[str, int] = {
    "actor_sub": 255,
    "actor_iss": 512,
    "on_behalf_of": 255,
    "source_ip": 64,
    "resource_type": 64,
    "resource_id": 255,
    "request_id": 128,
    "trace_id": 128,
}
"""Longest accepted length of each free-text column (printable characters only)."""


class ActorType(StrEnum):
    """Who acted: an authenticated user (token `sub` and `iss`) or a fixed system name."""

    USER = "user"
    SYSTEM = "system"


class Outcome(StrEnum):
    """How the action ended."""

    SUCCESS = "success"
    DENIED = "denied"
    FAILURE = "failure"


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One event to append. `occurred_at`, `recorded_at` and `seq` are the database's.

    `id` is generated here because an insert never uses `RETURNING` (a null-tenant row is not
    selectable by the app role). `before` and `after` must already be redacted by the emitter's
    allow-list (`trustbridge.audit.redact`); this layer checks only their shape and size.
    """

    actor_type: ActorType
    actor_sub: str
    action: Action
    resource_type: str
    outcome: Outcome
    actor_iss: str | None = None
    tenant_id: UUID | None = None
    on_behalf_of: str | None = None
    source_ip: str | None = None
    resource_id: str | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    request_id: str | None = None
    trace_id: str | None = None
    schema_version: int = SCHEMA_VERSION
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        """Validate every value (types, enums, lengths, shapes); a bad one raises `ValueError`.

        Fail closed: an event that cannot be validated is never built, so it is never written.
        `action` may be given as the string value of an `Action` and is stored as the member.
        """
        if not isinstance(self.actor_type, ActorType):
            raise ValueError("actor_type must be an ActorType")
        if not isinstance(self.outcome, Outcome):
            raise ValueError("outcome must be an Outcome")
        try:
            object.__setattr__(self, "action", Action(self.action))
        except ValueError:
            raise ValueError(f"action {self.action!r} is not a known audit action") from None
        for name, limit in TEXT_LIMITS.items():
            _check_text(name, getattr(self, name), limit)
        self._check_actor()
        self._check_ids_and_version()
        _check_payload("before", self.before)
        _check_payload("after", self.after)

    def _check_actor(self) -> None:
        """The actor columns agree with the actor type (and the table's check constraint)."""
        if not self.actor_sub:
            raise ValueError("actor_sub is required")
        if not self.resource_type:
            raise ValueError("resource_type is required")
        if self.actor_type is ActorType.USER and not self.actor_iss:
            raise ValueError("a user actor needs actor_iss")
        if self.actor_type is ActorType.SYSTEM:
            if self.actor_iss is not None:
                raise ValueError("a system actor has no actor_iss")
            if self.actor_sub not in {a.value for a in SystemActor}:
                raise ValueError(f"{self.actor_sub!r} is not a system actor name")

    def _check_ids_and_version(self) -> None:
        """Ids are UUIDs, the address is an IP without a scope id, the version is known."""
        for name in ("tenant_id", "id"):
            value = getattr(self, name)
            if not isinstance(value, UUID) and not (name == "tenant_id" and value is None):
                raise ValueError(f"{name} must be a UUID")
        if self.source_ip is not None:
            if "%" in self.source_ip:
                raise ValueError("source_ip must not carry a scope id")
            ipaddress.ip_address(self.source_ip)  # raises ValueError
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or not 1 <= self.schema_version <= SCHEMA_VERSION
        ):
            raise ValueError(f"schema_version must be an integer from 1 to {SCHEMA_VERSION}")


def _check_text(name: str, value: object, limit: int) -> None:
    """A text column is a printable string of at most `limit` characters, or None."""
    if value is None:
        return
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if len(value) > limit:
        raise ValueError(f"{name} is longer than {limit} characters")
    if not value.isprintable():
        raise ValueError(f"{name} contains a control character")


def _check_payload(name: str, value: object) -> None:
    """`before` and `after` are None or a JSON object that Postgres `jsonb` accepts, bounded."""
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    try:
        dumped = json.dumps(value, allow_nan=False)
    except TypeError, ValueError:
        raise ValueError(f"{name} is not serializable as JSON") from None
    if len(dumped.encode()) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"{name} is larger than {MAX_PAYLOAD_BYTES} bytes")
    if "\\u0000" in dumped:
        raise ValueError(f"{name} contains a NUL character")


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One stored event, as read back."""

    id: UUID
    seq: int
    occurred_at: datetime
    recorded_at: datetime
    tenant_id: UUID | None
    actor_type: ActorType
    actor_sub: str
    actor_iss: str | None
    on_behalf_of: str | None
    source_ip: str | None
    action: str
    resource_type: str
    resource_id: str | None
    outcome: Outcome
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    request_id: str | None
    trace_id: str | None
    schema_version: int

    def to_json(self) -> dict[str, Any]:
        """The export shape: every column, ids and timestamps as strings (UTC, `Z`)."""
        return {
            "id": str(self.id),
            "seq": self.seq,
            "occurred_at": _iso(self.occurred_at),
            "recorded_at": _iso(self.recorded_at),
            "tenant_id": None if self.tenant_id is None else str(self.tenant_id),
            "actor_type": self.actor_type.value,
            "actor_sub": self.actor_sub,
            "actor_iss": self.actor_iss,
            "on_behalf_of": self.on_behalf_of,
            "source_ip": self.source_ip,
            "action": self.action,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "outcome": self.outcome.value,
            "before": self.before,
            "after": self.after,
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "schema_version": self.schema_version,
        }

    def to_ndjson_line(self) -> bytes:
        """One NDJSON line: compact JSON, ASCII-escaped, terminated by a single newline."""
        return json.dumps(self.to_json(), separators=(",", ":")).encode() + b"\n"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class AuditQuery:
    """Filters and the cursor for a list or an export.

    The cursor is `seq`: ascending reads resume with `after_seq` (the last `seq` seen),
    descending reads with `before_seq`. `since` and `until` bound `occurred_at`
    (`since` inclusive, `until` exclusive). `lag_seconds` makes the read stop before the first
    event recorded within that many seconds (the export's consistency model).
    """

    after_seq: int | None = None
    before_seq: int | None = None
    tenant_id: UUID | None = None
    since: datetime | None = None
    until: datetime | None = None
    actor_sub: str | None = None
    action: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    outcome: Outcome | None = None
    lag_seconds: float | None = None
    descending: bool = False
    limit: int = 100

    def __post_init__(self) -> None:
        """Bound the page size and the lag."""
        if not 1 <= self.limit <= MAX_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
        if self.lag_seconds is not None and self.lag_seconds < 0:
            raise ValueError("lag_seconds must not be negative")


@dataclass(frozen=True, slots=True)
class AuditPage:
    """A page of records and, when more follow, the `seq` to continue from."""

    items: list[AuditRecord]
    next_seq: int | None
