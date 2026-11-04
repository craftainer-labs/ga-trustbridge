"""Writing events from call sites (ADR 0032): the writer, event builders and the sinks.

* `AuditWriter.record` writes one event in its **own** short transaction, opened inside the
  event's tenant context (ADR 0031: the insert policy admits the context tenant or null). Use it
  for events that have no surrounding transaction to join (a denial, a sync, a sweep).
* An event that must commit **with** a change is appended with `append(session, event)` in the
  change's own session instead (see `trustbridge.audit.credentials`); never from a second session.
* `with_trace` fills `trace_id` from the current OpenTelemetry span (the writer does it for
  you). `AuditEvent` validates every value, so a bad value raises `ValueError` before anything
  is written (fail closed).
"""

from dataclasses import replace
from uuid import UUID

from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.audit.actions import Action, SystemActor
from trustbridge.audit.denial import Denial
from trustbridge.audit.model import MAX_DELIVERY_ID, TEXT_LIMITS, ActorType, AuditEvent, Outcome
from trustbridge.audit.redact import project
from trustbridge.audit.store import append
from trustbridge.tenancy.context import tenant_context


def _printable(value: str, limit: int) -> str:
    """`value` without non-printable characters, cut to `limit` (a hostile claim stays valid)."""
    return "".join(c for c in value if c.isprintable())[:limit]


def current_trace_id() -> str | None:
    """The current span's trace id as 32 hex characters, or None outside a valid span."""
    ctx = trace.get_current_span().get_span_context()
    return f"{ctx.trace_id:032x}" if ctx.is_valid else None


def with_trace(event: AuditEvent) -> AuditEvent:
    """`event` with `trace_id` set from the current span unless it already carries one."""
    if event.trace_id is not None:
        return event
    return replace(event, trace_id=current_trace_id())


class AuditWriter:
    """Writes events in their own transactions over a session factory."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """Use `factory` for one short session per event."""
        self._factory = factory

    async def record(self, event: AuditEvent) -> UUID:
        """Insert `event` and commit it, with the event's tenant (or none) as the context."""
        with tenant_context(event.tenant_id):
            async with self._factory() as session, session.begin():
                return await append(session, with_trace(event))


class TenantSyncAudit:
    """Records the tenant state an installation event left behind (`tenant.sync`).

    The sync re-reads GitHub and overwrites the state, so the event records the resulting
    state (`after`) and who asked: the system actor `github-webhook`, on behalf of the GitHub
    sender's numeric id and the delivery id (never the login), as a JSON object. It runs after
    the tenant change in its own transaction; a failure raises, the delivery is retried and the
    sync repeats, so the change is never left without an event (a retry may add a second one).
    """

    def __init__(self, writer: AuditWriter) -> None:
        """Write through `writer`."""
        self._writer = writer

    async def tenant_synced(
        self, *, tenant_id: UUID, state: str, delivery_id: str, sender_id: int | None
    ) -> None:
        """Write one `tenant.sync` event in the tenant's context."""
        on_behalf_of = (
            None
            if sender_id is None
            else {
                "github_sender_id": sender_id,
                "delivery_id": _printable(delivery_id, MAX_DELIVERY_ID),
            }
        )
        await self._writer.record(
            AuditEvent(
                actor_type=ActorType.SYSTEM,
                actor_sub=SystemActor.GITHUB_WEBHOOK.value,
                action=Action.TENANT_SYNC,
                resource_type="tenant",
                outcome=Outcome.SUCCESS,
                tenant_id=tenant_id,
                resource_id=str(tenant_id),
                after=project("tenant", {"state": state}),
                on_behalf_of=on_behalf_of,
            )
        )


class AuditDenials:
    """Records `authz.denied` for an authenticated principal (a `DenialSink`).

    The row has a **null** `tenant_id` (no tenant context exists yet and only instance-scoped
    auditors see denials) and its own transaction. A tenant route puts the requested tenant id
    in `resource_id` with `resource_type = tenant`; an instance route names the missing role
    (`resource_type = role`).
    """

    def __init__(self, writer: AuditWriter) -> None:
        """Write through `writer`."""
        self._writer = writer

    async def denied(self, denial: Denial) -> None:
        """Write one `authz.denied` event for `denial`."""
        if denial.scope == "tenant":
            resource_type, resource_id = "tenant", denial.tenant_id
        else:
            resource_type, resource_id = "role", denial.required_role
        await self._writer.record(
            AuditEvent(
                actor_type=ActorType.USER,
                actor_sub=_printable(denial.sub, TEXT_LIMITS["actor_sub"]),
                actor_iss=None
                if denial.iss is None
                else _printable(denial.iss, TEXT_LIMITS["actor_iss"]),
                action=Action.AUTHZ_DENIED,
                resource_type=resource_type,
                outcome=Outcome.DENIED,
                resource_id=resource_id,
                source_ip=denial.source_ip,
                request_id=denial.request_id,
                after=project(
                    "authz_denial",
                    {
                        "reason": denial.reason,
                        "required_role": denial.required_role,
                        "scope": denial.scope,
                    },
                ),
            )
        )
