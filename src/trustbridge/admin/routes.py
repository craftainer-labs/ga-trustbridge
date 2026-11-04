"""The admin API v1 read routes (ADR 0033, NG6d-1), mounted by `trustbridge.main`.

Every route declares its guard (`admin.guards`): authenticate, role check (and, for
`{tenant_id}` routes, the tenant context), then the principal's rate-limit bucket. The
handlers only read.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.admin.access import all_tenants, tenant_ref
from trustbridge.admin.cursor import MAX_SEQ, decode_cursor, encode_cursor, invalid_cursor, paginate
from trustbridge.admin.guards import guard, guard_any_grant
from trustbridge.admin.problems import AdminProblem
from trustbridge.admin.runtime import AdminRuntime, admin_runtime
from trustbridge.admin.schemas import (
    AuditEventOut,
    AuditEventPage,
    GrantOut,
    HostOut,
    HostPage,
    MeOut,
    ProblemOut,
    TenantOut,
    TenantPage,
    TenantRefOut,
)
from trustbridge.audit import AuditQuery, Outcome, list_events, record_export
from trustbridge.rbac import Principal, Role, get_principal, tenant_roles
from trustbridge.rbac.deps import client_ip, safe_request_id
from trustbridge.tenancy import TenantInfo, tenant_session

logger = logging.getLogger(__name__)
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
_PROBLEMS: dict[int | str, dict[str, Any]] = {
    code: {"model": ProblemOut, "description": "Problem Details (application/problem+json)"}
    for code in (400, 401, 403, 404, 422, 429)
}
router = APIRouter(prefix="/admin/v1", responses=_PROBLEMS)

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
    """The caller: `sub`, `iss`, role grants and the tenants those grants reach."""
    grants = sorted(principal.grants, key=lambda g: (g.role.value, g.host or "", g.org or ""))
    tenants: list[TenantInfo] = []
    if runtime.session_factory is not None:
        tenants = [
            t
            for t in await all_tenants(runtime.session_factory)
            if tenant_roles(principal, tenant_ref(t))
        ]
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
    )


_spec: dict[str, Any] = {}


def openapi_spec() -> dict[str, Any]:
    """The OpenAPI document of this router only (the template's routes stay out)."""
    if not _spec:
        _spec.update(
            get_openapi(
                title="trustbridge admin API",
                version="1.0.0",
                summary="Administrative API v1 of trustbridge",
                routes=router.routes,
            )
        )
    return _spec


@router.get("/openapi.json", dependencies=guard(None))
async def get_openapi_spec() -> dict[str, Any]:
    """The OpenAPI 3 document of the admin API v1 (any authenticated caller)."""
    return openapi_spec()


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
        effective_state="disabled" if info.admin_disabled_at is not None else info.state,
        deleting=info.deleting,
    )


@router.get("/tenants", response_model=TenantPage, dependencies=guard_any_grant())
async def list_tenants_route(
    principal: Annotated[Principal, Depends(get_principal)],
    runtime: Annotated[AdminRuntime, Depends(admin_runtime)],
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> TenantPage:
    """The tenants the caller may read, ordered by id."""
    rows = [
        t
        for t in await all_tenants(_factory(runtime))
        if Role.VIEWER in tenant_roles(principal, tenant_ref(t))
    ]
    page, next_cursor = paginate(rows, key=lambda t: str(t.id), limit=limit, cursor=cursor)
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
    for info in await all_tenants(_factory(runtime)):
        if info.id == tenant_id:
            return _tenant_out(info)
    raise AdminProblem(404, "not-found", "Tenant not found")


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
