"""Doubles for the deletion engine's unit tests: a record store and step slots."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import UUID, uuid4

from trustbridge.audit import AuditEvent
from trustbridge.deletion.model import (
    NEXT_STATE,
    DeletionRecord,
    Lease,
    LeaseLost,
    StepContext,
    StepOutcome,
)

TENANT = UUID("11111111-1111-4111-8111-111111111111")


def record(**overrides: Any) -> DeletionRecord:  # noqa: ANN401
    """A freshly claimed record (fence 1, state `requested`)."""
    values: dict[str, Any] = {
        "id": uuid4(),
        "tenant_id": TENANT,
        "host": "github.com",
        "installation_id": 42,
        "delete_platform_resources": False,
        "state": "requested",
        "step_cursor": {},
        "counts": {},
        "skipped": [],
        "fence": 1,
        "attempts": 0,
    }
    values.update(overrides)
    return DeletionRecord(**values)


@dataclass
class FakeStore:
    """Applies the same transition rules as the database functions, in memory."""

    rec: DeletionRecord | None
    calls: list[tuple[str, Any]] = field(default_factory=list)
    events: list[AuditEvent] = field(default_factory=list)
    tenants: list[UUID] = field(default_factory=list)
    lose_on: set[str] = field(default_factory=set)
    boom_on: dict[str, Exception] = field(default_factory=dict)
    final: str | None = None
    last_error: str | None = None
    delay: int | None = None

    def _guard(self, name: str) -> None:
        if name in self.boom_on:
            raise self.boom_on[name]
        if name in self.lose_on:
            raise LeaseLost

    async def claim(self, holder: str, ttl_seconds: int) -> DeletionRecord | None:
        """The scripted record, or None."""
        self.calls.append(("claim", (holder, ttl_seconds)))
        self._guard("claim")
        return self.rec

    async def renew(self, lease: Lease, ttl_seconds: int) -> None:
        """Count a renewal."""
        self.calls.append(("renew", ttl_seconds))
        self._guard("renew")

    async def release(self, lease: Lease) -> None:
        """Record a release."""
        self.calls.append(("release", lease))
        self._guard("release")

    async def retry(self, lease: Lease, error: str, delay_seconds: int) -> int:
        """Record the error and the backoff."""
        self.calls.append(("retry", (error, delay_seconds)))
        self._guard("retry")
        self.last_error, self.delay = error, delay_seconds
        if self.rec is not None:
            self.rec = replace(self.rec, attempts=self.rec.attempts + 1)
            return self.rec.attempts
        return 1

    async def advance(  # noqa: PLR0913
        self,
        lease: Lease,
        tenant_id: UUID,
        to_state: str,
        *,
        cursor: Mapping[str, Any] | None,
        counts: Mapping[str, int],
        skipped: Sequence[Mapping[str, Any]],
        events: Sequence[AuditEvent],
    ) -> None:
        """Validate the transition like `tb_advance_tenant_deletion` and keep the events."""
        self.calls.append(("advance", (to_state, cursor, dict(counts), list(skipped))))
        self.tenants.append(tenant_id)
        self._guard("advance")
        assert self.rec is not None
        assert to_state in {self.rec.state, NEXT_STATE[self.rec.state]}
        progressed = to_state != self.rec.state or (
            cursor is not None and cursor != self.rec.step_cursor
        )
        self.rec = replace(
            self.rec,
            state=to_state,
            counts=counts,
            skipped=skipped,
            step_cursor=cursor
            if cursor is not None
            else ({} if to_state != self.rec.state else self.rec.step_cursor),
            attempts=0 if progressed else self.rec.attempts,
        )
        self.events.extend(events)

    async def finish(
        self,
        lease: Lease,
        record: DeletionRecord,
        *,
        counts: Mapping[str, int],
        skipped: Sequence[Mapping[str, Any]],
        events: Sequence[AuditEvent],
    ) -> None:
        """Write `done` and keep the events."""
        self.calls.append(("finish", (dict(counts), list(skipped))))
        self._guard("finish")
        assert record.state == "database"
        self.final = "done"
        self.events.extend(events)

    async def fail(
        self, lease: Lease, tenant_id: UUID, reason: str, events: Sequence[AuditEvent]
    ) -> None:
        """Write `failed` and keep the events."""
        self.calls.append(("fail", reason))
        self.tenants.append(tenant_id)
        self._guard("fail")
        self.final = "failed"
        self.last_error = reason
        self.events.extend(events)

    def names(self) -> list[str]:
        """The call names in order."""
        return [name for name, _ in self.calls]


class ScriptedStep:
    """A step double: each call returns the next scripted outcome or raises it."""

    def __init__(self, *script: StepOutcome | Exception) -> None:
        """`script` is consumed in order; the last item repeats."""
        self._script = list(script) or [StepOutcome()]
        self.contexts: list[StepContext] = []
        self.on_run: Callable[[], None] | None = None

    async def run(self, ctx: StepContext) -> StepOutcome:
        """Return or raise the next scripted item."""
        self.contexts.append(ctx)
        if self.on_run is not None:
            self.on_run()
        item = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if isinstance(item, Exception):
            raise item
        return item


class Clock:
    """A settable monotonic clock."""

    def __init__(self) -> None:
        """Start at zero."""
        self.now = 0.0

    def __call__(self) -> float:
        """The current value."""
        return self.now
