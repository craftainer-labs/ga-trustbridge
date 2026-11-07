"""The admin API v1 read routes (ADR 0033, NG6d-1), mounted by `trustbridge.main`.

Every route declares its guard (`admin.guards`): authenticate, role check (and, for
`{tenant_id}` routes, the tenant context), then the principal's rate-limit bucket. The
handlers only read.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from functools import cache
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.admin import queries
from trustbridge.admin.access import tenants_page
from trustbridge.admin.cursor import MAX_SEQ, decode_cursor, encode_cursor, invalid_cursor, paginate
from trustbridge.admin.guards import BoundedRoute, emits, guard, guard_any_grant
from trustbridge.admin.problems import AdminProblem
from trustbridge.admin.runtime import AdminRuntime, admin_runtime
from trustbridge.admin.schemas import (
    AdapterPage,
    AuditEventOut,
    AuditEventPage,
    CredentialPage,
    DeliveryDetailOut,
    DeliveryPage,
    DisableIn,
    GrantOut,
    HostOut,
    HostPage,
    MeOut,
    OperationPage,
    ProblemOut,
    RepositoryPage,
    ResourcePage,
    TenantOut,
    TenantPage,
    TenantRefOut,
)
from trustbridge.audit import (
    Action,
    ActorType,
    AuditEvent,
    AuditQuery,
    Outcome,
    list_events,
    project,
    record_export,
    scrub_text,
)
from trustbridge.audit.emit import with_trace
from trustbridge.audit.store import append, get_event
from trustbridge.rbac import Principal, Role, get_principal, instance_roles
from trustbridge.rbac.deps import client_ip, safe_request_id
from trustbridge.tenancy import TenantInfo, get_tenant, tenant_session
from trustbridge.worker.secrets import SecretUnavailable

logger = logging.getLogger(__name__)
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
_PROBLEMS: dict[int | str, dict[str, Any]] = {
    code: {"model": ProblemOut, "description": "Problem Details (application/problem+json)"}
    for code in (400, 401, 403, 404, 422, 429)
}
router = APIRouter(prefix="/admin/v1", responses=_PROBLEMS, route_class=BoundedRoute)

Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT, description="Page size (1 to 200)")]
Cursor = Annotated[str | None, Query(max_length=512, description="Opaque cursor")]


def _factory(runtime: AdminRuntime) -> async_sessionmaker[AsyncSession]:
    if runtime.session_factory is None:
        raise AdminProblem(503, "unavailable", "No database is configured")
    return runtime.session_factory


# ---- /me, /openapi.json ---------------------------------------------------------------


@router.get("/me", response_model=MeOut, dependencies=guard(None))
async def get_me(
    principal: Annotated[Principal, Depends(get_principal)],
    runtime: Annotated[AdminRuntime, Depends(admin_runtime)],
) -> MeOut:
    """The caller: `sub`, `iss`, role grants and up to 200 of the tenants they reach.

    `tenants_truncated` is true when more exist (page through `GET /tenants`).
    """
    grants = sorted(principal.grants, key=lambda g: (g.role.value, g.host or "", g.org or ""))
    tenants: list[TenantInfo] = []
    if runtime.session_factory is not None:
        # The first page of the tenants the grants reach (by id); `GET /tenants` pages through
        # all of them. Bounded so an instance-wide grant does not load every tenant per call.
        tenants = await tenants_page(
            runtime.session_factory, principal, after=None, limit=MAX_LIMIT + 1
        )
    truncated = len(tenants) > MAX_LIMIT
    tenants = tenants[:MAX_LIMIT]
    return MeOut(
        sub=principal.sub,
        iss=principal.iss,
        grants=[
            GrantOut(
                role=g.role.value,
                scope="instance" if g.instance else "tenant",
                host=g.host,
                org=g.org,
            )
            for g in grants
        ],
        tenants=[TenantRefOut(id=t.id, host=t.host, org=t.org or None) for t in tenants],
        tenants_truncated=truncated,
    )


def build_spec() -> dict[str, Any]:
    """The OpenAPI document of this router only (the template's routes stay out)."""
    return get_openapi(
        title="trustbridge admin API",
        version="1.0.0",
        summary="Administrative API v1 of trustbridge",
        routes=router.routes,
    )


def snapshot_text() -> str:
    """The canonical spec text: what `docs/openapi/admin-v1.json` holds (ADR 0033 inv. 11)."""
    return json.dumps(build_spec(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


@cache
def _spec_body() -> bytes:
    """The served bytes: immutable, built on the first request (the routes are fixed by then)."""
    return snapshot_text().encode()


@router.get(
    "/openapi.json",
    dependencies=guard(None),
    responses={200: {"description": "The OpenAPI 3 document", "content": {"application/json": {}}}},
)
async def get_openapi_spec() -> Response:
    """The OpenAPI 3 document of the admin API v1 (any authenticated caller)."""
    return Response(_spec_body(), media_type="application/json")


# ---- hosts ----------------------------------------------------------------------------


def _hosts(runtime: AdminRuntime) -> list[HostOut]:
    return [
        HostOut(name=name, state=status.state.value, reason=status.reason)
        for name, status in sorted(runtime.hosts.statuses().items())
    ]


@router.get("/hosts", response_model=HostPage, dependencies=guard(Role.VIEWER))
async def list_hosts(
    runtime: Annotated[AdminRuntime, Depends(admin_runtime)],
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> HostPage:
    """Configured GitHub hosts and their state as the answering replica sees it (instance)."""
    page, next_cursor = paginate(_hosts(runtime), key=lambda h: h.name, limit=limit, cursor=cursor)
    return HostPage(items=page, next_cursor=next_cursor)


@router.get("/hosts/{host}", response_model=HostOut, dependencies=guard(Role.VIEWER))
async def get_host(host: str, runtime: Annotated[AdminRuntime, Depends(admin_runtime)]) -> HostOut:
    """One host (instance scope)."""
    for item in _hosts(runtime):
        if item.name == host:
            return item
    raise AdminProblem(404, "not-found", "Host not found")


# ---- tenants --------------------------------------------------------------------------


def _tenant_out(info: TenantInfo) -> TenantOut:
    return TenantOut(
        id=info.id,
        host=info.host,
        installation_id=info.installation_id,
        org=info.org or None,
        org_id=info.org_id,
        state=info.state,
        admin_disabled_at=info.admin_disabled_at,
        admin_disabled_by=info.admin_disabled_by,
        admin_disable_reason=info.admin_disable_reason,
        effective_state="disabled" if info.admin_disabled_at is not None else info.state,
        deleting=info.deleting,
    )


def _tenant_cursor(info: TenantInfo) -> str:
    return encode_cursor(str(info.id), str(info.id))


@router.get("/tenants", response_model=TenantPage, dependencies=guard_any_grant())
async def list_tenants_route(
    principal: Annotated[Principal, Depends(get_principal)],
    runtime: Annotated[AdminRuntime, Depends(admin_runtime)],
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> TenantPage:
    """The tenants the caller may read, ordered by id."""
    after: UUID | None = None
    if cursor is not None:
        position, _ident = decode_cursor(cursor)
        try:
            after = UUID(str(position))
        except ValueError:
            raise invalid_cursor() from None
    rows = await tenants_page(_factory(runtime), principal, after=after, limit=limit + 1)
    page = rows[:limit]
    next_cursor = _tenant_cursor(page[-1]) if len(rows) > limit else None
    return TenantPage(items=[_tenant_out(t) for t in page], next_cursor=next_cursor)


@router.get(
    "/tenants/{tenant_id}",
    response_model=TenantOut,
    dependencies=guard(Role.VIEWER, tenant_param="tenant_id"),
)
async def get_tenant_route(
    tenant_id: UUID, runtime: Annotated[AdminRuntime, Depends(admin_runtime)]
) -> TenantOut:
    """One tenant; an unknown or ungranted id is a 404."""
    info = await get_tenant(_factory(runtime), tenant_id)
    if info is None:
        raise AdminProblem(404, "not-found", "Tenant not found")
    return _tenant_out(info)


# ---- tenant disable and enable (ADR 0033 "Tenant disable") ---------------------------------

_SET_DISABLED = text("SELECT * FROM tb_set_tenant_disabled(:id, :by, :reason, :disable)")
_REASON_MAX = 500


def _admin_state(at: datetime | None, by: str | None, reason: str | None) -> dict[str, Any]:
    """The `before` or `after` of a disable or enable, through the `tenant_admin` allow-list."""
    return project(
        "tenant_admin",
        {"admin_disabled_at": at, "admin_disabled_by": by, "admin_disable_reason": reason},
    )


def _tenant_event(  # noqa: PLR0913 - one value per recorded fact
    request: Request,
    principal: Principal,
    tenant_id: UUID,
    action: Action,
    outcome: Outcome,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> AuditEvent:
    return AuditEvent(
        actor_type=ActorType.USER,
        actor_sub=principal.sub,
        actor_iss=principal.iss,
        tenant_id=tenant_id,
        action=action,
        resource_type="tenant",
        resource_id=str(tenant_id),
        outcome=outcome,
        before=before,
        after=after,
        source_ip=client_ip(request),
        request_id=safe_request_id(request),
    )


async def _set_disabled(  # noqa: PLR0913 - the request, the caller and what to do
    request: Request,
    principal: Principal,
    runtime: AdminRuntime,
    tenant_id: UUID,
    *,
    disable: bool,
    reason: str | None,
) -> TenantOut:
    """Disable or enable the tenant and audit it in the same transaction.

    `tb_set_tenant_disabled` is one row-locked write that returns the before and after values;
    a repeat disable keeps the first time, actor and reason, and a call that changes nothing
    still writes its `success` event (before equal to after). A failure rolls the change back
    and leaves a `failure` event in its own transaction.
    """
    action = Action.TENANT_DISABLE if disable else Action.TENANT_ENABLE
    factory = _factory(runtime)
    params = {"id": tenant_id, "by": principal.sub, "reason": reason or "", "disable": disable}
    try:
        async with factory() as session, session.begin():
            row = (await session.execute(_SET_DISABLED, params)).one()
            if not row.tenant_found:
                raise AdminProblem(404, "not-found", "Tenant not found")
            event = _tenant_event(
                request,
                principal,
                tenant_id,
                action,
                Outcome.SUCCESS,
                before=_admin_state(
                    row.before_disabled_at, row.before_disabled_by, row.before_disable_reason
                ),
                after=_admin_state(
                    row.after_disabled_at, row.after_disabled_by, row.after_disable_reason
                ),
            )
            await append(session, with_trace(event))
    except AdminProblem:
        raise
    except Exception:
        await _record_failure(request, principal, runtime, tenant_id, action)
        raise
    info = await get_tenant(factory, tenant_id)
    if info is None:
        raise AdminProblem(404, "not-found", "Tenant not found")
    return _tenant_out(info)


async def _record_failure(
    request: Request,
    principal: Principal,
    runtime: AdminRuntime,
    tenant_id: UUID,
    action: Action,
) -> None:
    """Best effort: a `failure` event in its own transaction; never hides the original error."""
    if runtime.writer is None:
        return
    try:
        await runtime.writer.record(
            _tenant_event(request, principal, tenant_id, action, Outcome.FAILURE)
        )
    except Exception:
        logger.exception("could not record the failed %s event", action.value)


@router.post(
    "/tenants/{tenant_id}/disable",
    response_model=TenantOut,
    dependencies=guard(Role.ADMIN, "mutation", tenant_param="tenant_id", instance_only=True),
)
@emits(Action.TENANT_DISABLE)
async def disable_tenant(
    tenant_id: UUID,
    body: DisableIn,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
    runtime: Annotated[AdminRuntime, Depends(admin_runtime)],
) -> TenantOut:
    """Disable a tenant (instance `admin`). Its webhooks are stored but not handled, and no new
    adapter operation starts; the installation sync does not change this. Idempotent: a tenant
    that is already disabled keeps its first time, actor and reason."""
    reason = scrub_text(body.reason, _REASON_MAX)
    return await _set_disabled(request, principal, runtime, tenant_id, disable=True, reason=reason)


@router.post(
    "/tenants/{tenant_id}/enable",
    response_model=TenantOut,
    dependencies=guard(Role.ADMIN, "mutation", tenant_param="tenant_id", instance_only=True),
)
@emits(Action.TENANT_ENABLE)
async def enable_tenant(
    tenant_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
    runtime: Annotated[AdminRuntime, Depends(admin_runtime)],
) -> TenantOut:
    """Enable a tenant again (instance `admin`). Idempotent: an enabled tenant is a no-op."""
    return await _set_disabled(request, principal, runtime, tenant_id, disable=False, reason=None)


# ---- tenant data: repositories, adapters, resources, credentials, operations ------------------


@router.get(
    "/tenants/{tenant_id}/repositories",
    response_model=RepositoryPage,
    dependencies=guard(Role.VIEWER, tenant_param="tenant_id"),
)
async def list_repositories(
    tenant_id: UUID,
    runtime: Annotated[AdminRuntime, Depends(admin_runtime)],
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> RepositoryPage:
    """The repositories the tenant's installation can see, by numeric id."""
    async with _factory(runtime)() as session, session.begin():
        items, next_cursor = await queries.repositories(
            session, tenant_id, limit=limit, cursor=cursor
        )
    return RepositoryPage(items=items, next_cursor=next_cursor)


@router.get(
    "/tenants/{tenant_id}/adapters",
    response_model=AdapterPage,
    dependencies=guard(Role.VIEWER, tenant_param="tenant_id"),
)
async def list_adapters(
    tenant_id: UUID,
    runtime: Annotated[AdminRuntime, Depends(admin_runtime)],
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> AdapterPage:
    """The enabled adapters: name, whether the tenant has connection data, capabilities.

    Never the connection data (ADR 0033 invariant 8): the store is only asked which entries
    exist.
    """
    rows = []
    if runtime.adapters is not None:
        try:
            rows = await runtime.adapters.for_tenant(tenant_id)
        except SecretUnavailable:
            logger.exception("adapters: the connection store cannot be listed")
            raise AdminProblem(503, "unavailable", "The connection store is unavailable") from None
    page, next_cursor = paginate(rows, key=lambda a: a.name, limit=limit, cursor=cursor)
    return AdapterPage(items=page, next_cursor=next_cursor)


@router.get(
    "/tenants/{tenant_id}/resources",
    response_model=ResourcePage,
    dependencies=guard(Role.VIEWER, tenant_param="tenant_id"),
)
async def list_resources(
    tenant_id: UUID,
    runtime: Annotated[AdminRuntime, Depends(admin_runtime)],
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> ResourcePage:
    """The tenant's non-secret adapter resources, by adapter and key."""
    async with _factory(runtime)() as session, session.begin():
        items, next_cursor = await queries.resources(session, tenant_id, limit=limit, cursor=cursor)
    return ResourcePage(items=items, next_cursor=next_cursor)


@router.get(
    "/tenants/{tenant_id}/credentials",
    response_model=CredentialPage,
    dependencies=guard(Role.VIEWER, tenant_param="tenant_id"),
)
async def list_credentials(
    tenant_id: UUID,
    runtime: Annotated[AdminRuntime, Depends(admin_runtime)],
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> CredentialPage:
    """The tenant's credential scopes: state, names and timestamps, never a value."""
    async with _factory(runtime)() as session, session.begin():
        items, next_cursor = await queries.credentials(
            session, tenant_id, limit=limit, cursor=cursor
        )
    return CredentialPage(items=items, next_cursor=next_cursor)


@router.get(
    "/tenants/{tenant_id}/operations",
    response_model=OperationPage,
    dependencies=guard(Role.VIEWER, tenant_param="tenant_id"),
)
async def list_operations(
    tenant_id: UUID,
    runtime: Annotated[AdminRuntime, Depends(admin_runtime)],
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> OperationPage:
    """The tenant's credential operations, newest first."""
    async with _factory(runtime)() as session, session.begin():
        items, next_cursor = await queries.operations(
            session, tenant_id, limit=limit, cursor=cursor
        )
    return OperationPage(items=items, next_cursor=next_cursor)


# ---- deliveries (host-scoped: instance roles only) -----------------------------------------


@router.get("/deliveries", response_model=DeliveryPage, dependencies=guard(Role.VIEWER))
async def list_deliveries(
    runtime: Annotated[AdminRuntime, Depends(admin_runtime)],
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> DeliveryPage:
    """Received webhook deliveries, newest first: status, attempts and error, no headers or body."""
    async with _factory(runtime)() as session, session.begin():
        items, next_cursor = await queries.deliveries(session, limit=limit, cursor=cursor)
    return DeliveryPage(items=items, next_cursor=next_cursor)


@router.get(
    "/deliveries/{host}/{delivery_id}",
    response_model=DeliveryDetailOut,
    dependencies=guard(Role.VIEWER),
)
async def get_delivery(
    host: Annotated[str, Path(max_length=63)],
    delivery_id: Annotated[str, Path(max_length=255)],
    principal: Annotated[Principal, Depends(get_principal)],
    runtime: Annotated[AdminRuntime, Depends(admin_runtime)],
) -> DeliveryDetailOut:
    """One delivery. The stored payload (never the headers) is returned only to an `operator`."""
    with_payload = Role.OPERATOR in instance_roles(principal)
    async with _factory(runtime)() as session, session.begin():
        found = await queries.delivery(session, host, delivery_id, with_payload=with_payload)
    if found is None:
        raise AdminProblem(404, "not-found", "Delivery not found")
    return found


# ---- audit ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuditFilters:
    """The `AuditQuery` filter fields, by their `AuditQuery` names."""

    since: datetime | None = None
    until: datetime | None = None
    actor_sub: str | None = None
    action: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    outcome: Outcome | None = None


def _aware(name: str, value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise AdminProblem(422, "validation", f"{name} needs a time zone (RFC 3339)")
    return value


def audit_filters(  # noqa: PLR0913 - one parameter per documented filter
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    actor_sub: Annotated[str | None, Query(max_length=255)] = None,
    action: Annotated[str | None, Query(max_length=64)] = None,
    resource_type: Annotated[str | None, Query(max_length=64)] = None,
    resource_id: Annotated[str | None, Query(max_length=255)] = None,
    outcome: Outcome | None = None,
) -> AuditFilters:
    """Dependency: the audit filters (`since` inclusive, `until` exclusive, on `occurred_at`)."""
    return AuditFilters(
        _aware("since", since),
        _aware("until", until),
        actor_sub,
        action,
        resource_type,
        resource_id,
        outcome,
    )


Filters = Annotated[AuditFilters, Depends(audit_filters)]
InstanceTenant = Annotated[UUID | None, Query(description="Narrow to one tenant")]


def _query(filters: AuditFilters, **extra: Any) -> AuditQuery:  # noqa: ANN401 - AuditQuery fields
    return AuditQuery(
        since=filters.since,
        until=filters.until,
        actor_sub=filters.actor_sub,
        action=filters.action,
        resource_type=filters.resource_type,
        resource_id=filters.resource_id,
        outcome=filters.outcome,
        **extra,
    )


async def _list_audit(  # noqa: PLR0913 - the documented query surface
    runtime: AdminRuntime,
    filters: AuditFilters,
    *,
    tenant_id: UUID | None,
    narrow: UUID | None,
    limit: int,
    cursor: str | None,
    descending: bool,
) -> AuditEventPage:
    query = _query(filters, limit=limit, descending=descending, tenant_id=narrow)
    if cursor is not None:
        position, _ident = decode_cursor(cursor)
        if not isinstance(position, int):
            raise invalid_cursor()
        query = (
            replace(query, before_seq=position)
            if descending
            else replace(query, after_seq=position)
        )
    async with _factory(runtime)() as session, session.begin():
        page = await list_events(
            session, query, tenant_id=tenant_id, instance_scope=tenant_id is None
        )
    items = [AuditEventOut.model_validate(record) for record in page.items]
    next_cursor = None
    if page.next_seq is not None:
        last = page.items[-1]
        next_cursor = encode_cursor(last.seq, str(last.id))
    return AuditEventPage(items=items, next_cursor=next_cursor)


@router.get(
    "/audit-events",
    response_model=AuditEventPage,
    dependencies=guard(Role.AUDITOR),
)
async def list_audit_events(  # noqa: PLR0913 - the documented query surface
    *,
    runtime: Annotated[AdminRuntime, Depends(admin_runtime)],
    filters: Filters,
    tenant_id: InstanceTenant = None,
    descending: bool = False,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> AuditEventPage:
    """Audit events of all tenants and the null-tenant events (instance-scoped auditor)."""
    return await _list_audit(
        runtime,
        filters,
        tenant_id=None,
        narrow=tenant_id,
        limit=limit,
        cursor=cursor,
        descending=descending,
    )


@router.get(
    "/tenants/{tenant_id}/audit-events",
    response_model=AuditEventPage,
    dependencies=guard(Role.AUDITOR, tenant_param="tenant_id"),
)
async def list_tenant_audit_events(  # noqa: PLR0913 - the documented query surface
    *,
    tenant_id: UUID,
    runtime: Annotated[AdminRuntime, Depends(admin_runtime)],
    filters: Filters,
    descending: bool = False,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> AuditEventPage:
    """One tenant's audit events (an auditor on that tenant, or an instance auditor)."""
    return await _list_audit(
        runtime,
        filters,
        tenant_id=tenant_id,
        narrow=None,
        limit=limit,
        cursor=cursor,
        descending=descending,
    )


@asynccontextmanager
async def _read_session(
    factory: async_sessionmaker[AsyncSession], tenant_id: UUID | None
) -> AsyncIterator[AsyncSession]:
    """A short read transaction, in the tenant's context for a tenant-scoped export."""
    if tenant_id is None:
        async with factory() as session, session.begin():
            yield session
    else:
        async with tenant_session(factory, tenant_id) as session, session.begin():
            yield session


async def _export_stream(  # noqa: PLR0913 - everything the event records
    *,
    runtime: AdminRuntime,
    principal: Principal,
    query: AuditQuery,
    tenant_id: UUID | None,
    source_ip: str | None,
    request_id: str | None,
) -> AsyncIterator[bytes]:
    """NDJSON lines in `seq` order, one short transaction per page; then the `audit.export` event.

    The event is written in `finally`, so a client that went away or a failed page still leaves
    a record (`complete: false`, outcome `failure`, `last_seq` to resume from).
    """
    assert runtime.writer is not None  # noqa: S101 - checked before the response starts
    factory = _factory(runtime)
    current = replace(
        query,
        descending=False,
        lag_seconds=runtime.export_lag_seconds,
        limit=runtime.export_page_size,
    )
    count, last_seq, complete = 0, None, False
    try:
        while True:
            async with _read_session(factory, tenant_id) as session:
                page = await list_events(
                    session, current, tenant_id=tenant_id, instance_scope=tenant_id is None
                )
            for record in page.items:
                count, last_seq = count + 1, record.seq
                yield record.to_ndjson_line()
            if page.next_seq is None:
                break
            current = replace(current, after_seq=page.next_seq)
        complete = True
    finally:
        try:
            await asyncio.shield(
                record_export(
                    runtime.writer,
                    sub=principal.sub,
                    iss=principal.iss,
                    tenant_id=tenant_id,
                    query=query,
                    row_count=count,
                    last_seq=last_seq,
                    complete=complete,
                    source_ip=source_ip,
                    request_id=request_id,
                )
            )
        except Exception:
            # The response is already streaming; the failed audit write must not mask the
            # original error, so it is logged loudly instead.
            logger.exception("audit.export row not written; %d rows were sent", count)


def _export_response(
    request: Request,
    runtime: AdminRuntime,
    principal: Principal,
    query: AuditQuery,
    tenant_id: UUID | None,
) -> StreamingResponse:
    if runtime.writer is None:
        raise AdminProblem(503, "unavailable", "No database is configured")
    scope = "instance" if tenant_id is None else str(tenant_id)
    return StreamingResponse(
        _export_stream(
            runtime=runtime,
            principal=principal,
            query=query,
            tenant_id=tenant_id,
            source_ip=client_ip(request),
            request_id=safe_request_id(request),
        ),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="audit-events-{scope}.ndjson"'},
    )


_EXPORT_RESPONSE: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "One audit event per line (NDJSON), ordered by seq",
        "content": {"application/x-ndjson": {"schema": {"type": "string"}}},
    }
}
SinceSeq = Annotated[
    int | None, Query(ge=0, le=MAX_SEQ, description="Resume after this seq (exclusive)")
]


@router.get(
    "/audit-events/export",
    response_class=StreamingResponse,
    responses=_EXPORT_RESPONSE,
    dependencies=guard(Role.AUDITOR, "export"),
)
async def export_audit_events(  # noqa: PLR0913 - the documented query surface
    *,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
    runtime: Annotated[AdminRuntime, Depends(admin_runtime)],
    filters: Filters,
    tenant_id: InstanceTenant = None,
    since_seq: SinceSeq = None,
) -> StreamingResponse:
    """Stream all tenants' audit events as NDJSON (instance-scoped auditor); itself audited."""
    query = _query(filters, tenant_id=tenant_id, after_seq=since_seq)
    return _export_response(request, runtime, principal, query, None)


@router.get(
    "/tenants/{tenant_id}/audit-events/export",
    response_class=StreamingResponse,
    responses=_EXPORT_RESPONSE,
    dependencies=guard(Role.AUDITOR, "export", tenant_param="tenant_id"),
)
async def export_tenant_audit_events(  # noqa: PLR0913 - the documented query surface
    *,
    request: Request,
    tenant_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    runtime: Annotated[AdminRuntime, Depends(admin_runtime)],
    filters: Filters,
    since_seq: SinceSeq = None,
) -> StreamingResponse:
    """Stream one tenant's audit events as NDJSON; itself audited."""
    query = _query(filters, after_seq=since_seq)
    return _export_response(request, runtime, principal, query, tenant_id)


# Registered after the `/audit-events/export` routes: a fixed segment must win over `{event_id}`.
@router.get(
    "/audit-events/{event_id}",
    response_model=AuditEventOut,
    dependencies=guard(Role.AUDITOR),
)
async def get_audit_event(
    event_id: UUID, runtime: Annotated[AdminRuntime, Depends(admin_runtime)]
) -> AuditEventOut:
    """One audit event of any tenant, or a null-tenant event (instance-scoped auditor)."""
    async with _factory(runtime)() as session, session.begin():
        record = await get_event(session, event_id)
    if record is None:
        raise AdminProblem(404, "not-found", "Audit event not found")
    return AuditEventOut.model_validate(record)
