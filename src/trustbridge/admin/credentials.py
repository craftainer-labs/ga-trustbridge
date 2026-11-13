"""Credential rotation through the admin API (ADR 0033, ADR 0029, NG6e-5).

`rotate` runs the credential core synchronously in the api process: it takes the scope's lease,
writes the write-ahead operation, asks the adapter for a new secret, parks it, delivers it to
GitHub and (two-phase adapters) leaves the old one to be retired later. The core's own state
changes are audited in their transaction by `CredentialAuditObserver` (`credential.rotate.begin`
and `.finish`, actor `api`); the call itself leaves exactly one `credential.rotate` event with the
caller as actor and the operation's id, so the two can be joined.

No secret value is returned, recorded or logged here: the answer holds ids, statuses and an
audit-safe message the adapter wrote, and every failure is reported with a fixed text (`from None`,
so no exception that may carry secret material is chained).
"""

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request

from trustbridge.adapters.core import (
    AdapterRegistry,
    CoreDeps,
    CredentialCore,
    OperationFailedError,
    RetryableOperationError,
    RotationNotApplicableError,
    TenantDisabledError,
)
from trustbridge.adapters.interface import (
    AdapterConfigurationError,
    AdapterError,
    PlatformAdapter,
)
from trustbridge.adapters.sql_store import SqlCredentialStore
from trustbridge.adapters.store import (
    CredentialStore,
    LeaseLostError,
    OutputError,
    TenantResolver,
)
from trustbridge.admin import views
from trustbridge.admin.problems import AdminProblem
from trustbridge.audit import Action, ActorType, AuditEvent, AuditWriter, Outcome, project
from trustbridge.audit.actions import SystemActor
from trustbridge.audit.credentials import (
    CredentialAuditObserver,
    JoinedCallEvent,
    join_call_event,
)
from trustbridge.audit.redact import FailureReason, failure_after
from trustbridge.github.availability import HostNotReadyError
from trustbridge.github.client import GitHubApiError
from trustbridge.github.writer import GitHubEnvironmentInspector, GitHubOutputWriter
from trustbridge.rbac import Principal
from trustbridge.rbac.deps import client_ip, safe_request_id
from trustbridge.tenancy.gate import SqlTenantGate
from trustbridge.worker.lease import LeaseBusy, LeaseLost
from trustbridge.worker.ports import UnknownTenantError
from trustbridge.worker.runtime import Core
from trustbridge.worker.secrets import SecretUnavailable

logger = logging.getLogger(__name__)


class RotationOut(BaseModel):
    """What a rotation did. Never a credential value."""

    operation_id: UUID | None
    status: str | None
    change: str
    wait_until: datetime | None
    manual_action: str | None


@dataclass(frozen=True, slots=True)
class CredentialRuntime:
    """The credential core in the api process (`AdminRuntime.credentials`)."""

    core: CredentialCore
    store: CredentialStore
    tenants: TenantResolver
    adapters: frozenset[str]
    """The enabled adapters; an adapter outside it is a 404."""


@dataclass(frozen=True, slots=True)
class Call:
    """One rotate call: the request, the caller, what to rotate and where to write."""

    request: Request
    principal: Principal
    tenant_id: UUID
    adapter: str
    scope_key: str
    writer: AuditWriter

    def event(self, outcome: Outcome, after: Mapping[str, object] | None = None) -> AuditEvent:
        """The `credential.rotate` event: the scope key is the resource, the caller the actor."""
        return AuditEvent(
            actor_type=ActorType.USER,
            actor_sub=self.principal.sub,
            actor_iss=self.principal.iss,
            tenant_id=self.tenant_id,
            action=Action.CREDENTIAL_ROTATE,
            resource_type="credential",
            resource_id=self.scope_key,
            outcome=outcome,
            after=dict(after) if after is not None else None,
            source_ip=client_ip(self.request),
            request_id=safe_request_id(self.request),
        )


async def _record(call: Call, outcome: Outcome, after: Mapping[str, object] | None = None) -> None:
    await call.writer.record(call.event(outcome, after))


async def _failure(call: Call, joined: JoinedCallEvent, reason: FailureReason) -> None:
    """Best effort: a `failure` event in its own transaction; never hides the original error.

    The event carries a `FailureReason` code, never a message. When the finishing save already
    committed the call's success event (an error after it, e.g. releasing the lease), no failure
    event is added: the call leaves exactly one event, and the committed one is the truth.
    """
    if joined.written:
        logger.warning("rotate error after the finishing save; its success event stands")
        return
    try:
        await _record(call, Outcome.FAILURE, failure_after(reason))
    except Exception:
        logger.exception("could not record the failed %s event", Action.CREDENTIAL_ROTATE.value)


def _upstream_reason(exc: Exception) -> FailureReason:
    """Which upstream failed, by type only."""
    if isinstance(exc, HostNotReadyError):
        return FailureReason.HOST_NOT_READY
    if isinstance(exc, SecretUnavailable):
        return FailureReason.STORE_UNAVAILABLE
    return FailureReason.UPSTREAM


async def rotate(call: Call, rt: CredentialRuntime) -> RotationOut:
    """Rotate the credential scope, or resume its open operation (idempotent).

    404 for an adapter that is not enabled or a scope with no credential; 409 for a disabled
    tenant, a scope with nothing rotatable, a busy lease or an adapter that is not configured; 502
    (fixed text) when the platform or GitHub fails. Each outcome leaves exactly one event.
    """
    joined = JoinedCallEvent(
        call.tenant_id,
        call.scope_key,
        lambda operation_id: call.event(Outcome.SUCCESS, _success_after(call, operation_id)),
    )
    try:
        if call.adapter not in rt.adapters:
            raise AdminProblem(404, "not-found", "Adapter not found")
        stored = await rt.store.get_credential(call.tenant_id, call.adapter, call.scope_key)
        if stored is None:
            raise AdminProblem(404, "not-found", "Credential not found")
        tenant = await rt.tenants.get_tenant(call.tenant_id)
        # The save that finishes the rotation appends the call's success event in its own
        # transaction (ADR 0032 invariant 9); `joined.written` says it did.
        with join_call_event(joined):
            result = await rt.core.rotate_scope(tenant, call.adapter, stored.scope)
    except AdminProblem:
        await _failure(call, joined, FailureReason.NOT_FOUND)
        raise
    except UnknownTenantError:
        await _failure(call, joined, FailureReason.NOT_FOUND)
        raise AdminProblem(404, "not-found", "Tenant not found") from None
    except TenantDisabledError:
        await _failure(call, joined, FailureReason.TENANT_DISABLED)
        raise AdminProblem(409, "conflict", "The tenant is disabled") from None
    except RotationNotApplicableError:
        await _failure(call, joined, FailureReason.NOT_ROTATABLE)
        raise AdminProblem(409, "conflict", "The credential has no rotatable secret") from None
    except LeaseBusy:
        await _failure(call, joined, FailureReason.LEASE_BUSY)
        raise AdminProblem(
            409, "conflict", "The credential is being changed; retry shortly"
        ) from None
    except LeaseLost, LeaseLostError:
        await _failure(call, joined, FailureReason.LEASE_LOST)
        raise AdminProblem(
            409, "conflict", "The credential is being changed; retry shortly"
        ) from None
    except AdapterConfigurationError:
        await _failure(call, joined, FailureReason.NOT_CONFIGURED)
        raise AdminProblem(
            409, "conflict", "The adapter is not configured for this tenant"
        ) from None
    except (
        RetryableOperationError,
        OperationFailedError,
        AdapterError,
        OutputError,
        GitHubApiError,
        HostNotReadyError,
        SecretUnavailable,
    ) as exc:
        logger.warning("rotation failed for tenant %s: %s", call.tenant_id, type(exc).__name__)
        await _failure(call, joined, _upstream_reason(exc))
        raise AdminProblem(
            502, "bad-gateway", "The platform or GitHub failed to complete the rotation"
        ) from None
    except asyncio.CancelledError:
        # The client went away mid-rotation: the operation stays open for the reconciler, but the
        # call still leaves its event (shielded, so the cancellation cannot cut the write short).
        await asyncio.shield(_failure(call, joined, FailureReason.CANCELLED))
        raise
    except Exception as exc:
        # Unmapped: never let the chain (which may hold a secret) reach the error logger.
        logger.error("rotation crashed for tenant %s: %s", call.tenant_id, type(exc).__name__)
        await _failure(call, joined, FailureReason.INTERNAL)
        raise AdminProblem(500, "internal", "The rotation failed unexpectedly") from None
    await _record_success(call, result.operation_id, joined)
    return RotationOut(
        operation_id=result.operation_id,
        status=None if result.status is None else result.status.value,
        change=result.change.value,
        wait_until=result.wait_until,
        manual_action=views.free_text(result.manual_action),
    )


def _success_after(call: Call, operation_id: UUID | None) -> dict[str, object]:
    """The success event's `after`, through the `credential` allow-list."""
    return project(
        "credential",
        {
            "adapter": call.adapter,
            "operation_kind": "rotate",
            **({} if operation_id is None else {"operation_id": operation_id}),
        },
    )


async def _record_success(call: Call, operation_id: UUID | None, joined: JoinedCallEvent) -> None:
    """The success event of a call that did not finish the rotation; if it fails the call does.

    A rotation this call finished never gets here: its event commits with the finishing save
    (`JoinedCallEvent`), so a finished change is never left unaudited and never reported as a
    success without its event. What reaches here is an open two-phase rotation or a manual step,
    which stays resumable: the retry resumes the same operation instead of rotating again.
    """
    if joined.written:
        return
    await _record(call, Outcome.SUCCESS, _success_after(call, operation_id))


def build_runtime(
    *,
    classes: Mapping[str, type[PlatformAdapter]],
    core: Core,
    sessions: async_sessionmaker[AsyncSession] | None,
    writer: AuditWriter | None,
) -> CredentialRuntime | None:
    """The credential core for the api process, or None without a database.

    The same parts the worker would use (ADR 0029): the enabled adapter classes over the tenants'
    connection data, the SQL credential store whose saves append the credential events (actor
    `api`), the lease manager, parked values, the GitHub output writer and environment inspector,
    and the admin-disable gate, so a disabled tenant starts no operation.
    """
    if sessions is None or writer is None:
        return None
    observer = CredentialAuditObserver(writer, SystemActor.API)
    store = SqlCredentialStore(sessions, observer)
    gateway = core.runtime.gateway
    deps = CoreDeps(
        registry=AdapterRegistry(classes, core.connections),
        store=store,
        values=core.parked,
        leases=core.leases,
        writer=GitHubOutputWriter(gateway),
        inspector=GitHubEnvironmentInspector(gateway),
        tenants=core.tenants,
        orphans=observer,
        gate=SqlTenantGate(sessions),
    )
    return CredentialRuntime(CredentialCore(deps), store, core.tenants, frozenset(classes))
