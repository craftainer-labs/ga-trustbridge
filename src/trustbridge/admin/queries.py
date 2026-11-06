"""The reads behind the tenant-scoped and delivery routes (ADR 0033, NG6d-2).

Every function takes the caller's session (opened and ended by the route) and returns one page
of response models plus the next cursor. Pagination is keyset: the cursor names the last row
sent (`cursor.py`), so rows added or removed between pages are neither repeated nor skipped.
Tenant-scoped reads also filter `tenant_id` explicitly, on top of the tenant context the guard
set (row-level security). Statements are constants; only bound parameters vary.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from trustbridge.admin import views
from trustbridge.admin.cursor import decode_cursor, encode_cursor, invalid_cursor
from trustbridge.admin.schemas import (
    CredentialOut,
    DeliveryDetailOut,
    DeliveryOut,
    OperationOut,
    RepositoryOut,
    ResourceOut,
)

_REPOSITORIES = text(
    "SELECT tenant_id, repository_id, full_name FROM tenant_repositories "
    "WHERE tenant_id = :tenant_id AND repository_id > :after ORDER BY repository_id LIMIT :limit"
)
_RESOURCES = text(
    "SELECT tenant_id, adapter, resource_key, state, updated_at FROM adapter_resources "
    "WHERE tenant_id = :tenant_id AND (adapter, resource_key) > (:adapter, :key) "
    "ORDER BY adapter, resource_key LIMIT :limit"
)
_CREDENTIALS = text(
    "SELECT tenant_id, adapter, scope_key, scope, kind, state, outputs, updated_at "
    "FROM adapter_credentials "
    "WHERE tenant_id = :tenant_id AND (adapter, scope_key) > (:adapter, :key) "
    "ORDER BY adapter, scope_key LIMIT :limit"
)
_OPERATION_COLUMNS = (
    "id, tenant_id, adapter, scope_key, kind, status, oidc_offered, base_state, base_kind, "
    "attempts, error, manual_action, created_at, updated_at"
)
_OPERATIONS = text(
    f"SELECT {_OPERATION_COLUMNS} FROM credential_operations "  # noqa: S608 -- constants only
    "WHERE tenant_id = :tenant_id "
    "AND (CAST(:since AS timestamptz) IS NULL "
    "OR (created_at, id) < (CAST(:since AS timestamptz), CAST(:id AS uuid))) "
    "ORDER BY created_at DESC, id DESC LIMIT :limit"
)
_DELIVERY_COLUMNS = (
    "host, delivery_id, event, status, attempts, received_at, next_attempt_at, last_relayed_at, "
    "processed_at, dead_at, last_error, body_sha256, body IS NOT NULL AS has_body"
)
_DELIVERIES = text(
    f"SELECT {_DELIVERY_COLUMNS} FROM webhook_deliveries "  # noqa: S608 -- constants only
    "WHERE (CAST(:since AS timestamptz) IS NULL "
    "OR (received_at, host, delivery_id) < (CAST(:since AS timestamptz), :host, :delivery_id)) "
    "ORDER BY received_at DESC, host DESC, delivery_id DESC LIMIT :limit"
)
# The body is selected only here, and never the headers column.
_DELIVERY = text(
    f"SELECT {_DELIVERY_COLUMNS}, CASE WHEN CAST(:with_body AS boolean) THEN body END AS body "  # noqa: S608 -- constants only
    "FROM webhook_deliveries WHERE host = :host AND delivery_id = :delivery_id"
)
_EPOCH_ID = UUID(int=0)


def _json_columns(row: Mapping[Any, Any], *names: str) -> dict[str, Any]:
    """The row as a dict, with the named JSONB columns decoded if the driver left them as text."""
    values = dict(row)
    for name in names:
        if isinstance(values[name], str | bytes):
            values[name] = json.loads(values[name])
    return values


def _resource(row: Mapping[Any, Any]) -> ResourceOut:
    values = _json_columns(row, "state")
    return ResourceOut(**{**values, "state": views.resource_state(values["state"])})


def _credential(row: Mapping[Any, Any]) -> CredentialOut:
    values = _json_columns(row, "scope", "state", "outputs")
    return CredentialOut(
        **{
            **values,
            "scope": views.scope(values["scope"]),
            "state": views.credential_state(values["state"]),
            "outputs": views.outputs(values["outputs"]),
        }
    )


def _operation(row: Mapping[Any, Any]) -> OperationOut:
    values = _json_columns(row, "base_state")
    return OperationOut(
        **{
            **values,
            "base_state": views.optional_credential_state(values["base_state"]),
            "error": views.free_text(values["error"]),
            "manual_action": views.free_text(values["manual_action"]),
        }
    )


def _delivery_values(row: Mapping[Any, Any]) -> dict[str, Any]:
    """The row with `last_error` scrubbed (a viewer sees it, as the audit log would)."""
    values = dict(row)
    values["last_error"] = views.free_text(values["last_error"])
    return values


def _cut[T](
    rows: Sequence[T], limit: int, cursor_of: Callable[[T], str]
) -> tuple[list[T], str | None]:
    """The first `limit` of the up-to-`limit + 1` rows fetched, and the cursor if more remain."""
    page = list(rows[:limit])
    return page, cursor_of(page[-1]) if len(rows) > limit else None


def _int_position(cursor: str | None) -> int:
    if cursor is None:
        return 0
    position, _ident = decode_cursor(cursor)
    if not isinstance(position, int):
        raise invalid_cursor()
    return position


def _text_pair(cursor: str | None) -> tuple[str, str]:
    """`(adapter, key)` after which to resume; the empty pair sorts before every row."""
    if cursor is None:
        return "", ""
    first, second = decode_cursor(cursor)
    if not isinstance(first, str):
        raise invalid_cursor()
    return first, second


def _time_pair(cursor: str | None) -> tuple[datetime | None, str]:
    """`(time, id)` after which to resume, or `(None, "")` for the first page."""
    if cursor is None:
        return None, ""
    first, second = decode_cursor(cursor)
    if not isinstance(first, str):
        raise invalid_cursor()
    try:
        moment = datetime.fromisoformat(first)
    except ValueError:
        raise invalid_cursor() from None
    if moment.tzinfo is None:
        raise invalid_cursor()
    return moment, second


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise invalid_cursor() from None


async def repositories(
    session: AsyncSession, tenant_id: UUID, *, limit: int, cursor: str | None
) -> tuple[list[RepositoryOut], str | None]:
    """The tenant's repositories by numeric id."""
    after = _int_position(cursor)
    rows = (
        await session.execute(
            _REPOSITORIES, {"tenant_id": tenant_id, "after": after, "limit": limit + 1}
        )
    ).mappings()
    items = [RepositoryOut(**row) for row in rows]
    return _cut(items, limit, lambda r: encode_cursor(r.repository_id, str(r.repository_id)))


async def resources(
    session: AsyncSession, tenant_id: UUID, *, limit: int, cursor: str | None
) -> tuple[list[ResourceOut], str | None]:
    """The tenant's adapter resources by (adapter, resource key)."""
    adapter, key = _text_pair(cursor)
    rows = (
        await session.execute(
            _RESOURCES,
            {"tenant_id": tenant_id, "adapter": adapter, "key": key, "limit": limit + 1},
        )
    ).mappings()
    items = [_resource(row) for row in rows]
    return _cut(items, limit, lambda r: encode_cursor(r.adapter, r.resource_key))


async def credentials(
    session: AsyncSession, tenant_id: UUID, *, limit: int, cursor: str | None
) -> tuple[list[CredentialOut], str | None]:
    """The tenant's credential scopes by (adapter, scope key); state and names only."""
    adapter, key = _text_pair(cursor)
    rows = (
        await session.execute(
            _CREDENTIALS,
            {"tenant_id": tenant_id, "adapter": adapter, "key": key, "limit": limit + 1},
        )
    ).mappings()
    items = [_credential(row) for row in rows]
    return _cut(items, limit, lambda r: encode_cursor(r.adapter, r.scope_key))


async def operations(
    session: AsyncSession, tenant_id: UUID, *, limit: int, cursor: str | None
) -> tuple[list[OperationOut], str | None]:
    """The tenant's credential operations, newest first."""
    since, ident = _time_pair(cursor)
    rows = (
        await session.execute(
            _OPERATIONS,
            {
                "tenant_id": tenant_id,
                "since": since,
                "id": _uuid(ident) if since is not None else _EPOCH_ID,
                "limit": limit + 1,
            },
        )
    ).mappings()
    items = [_operation(row) for row in rows]
    return _cut(items, limit, lambda r: encode_cursor(r.created_at.isoformat(), str(r.id)))


def _delivery_cursor(item: DeliveryOut) -> str:
    return encode_cursor(item.received_at.isoformat(), f"{item.host}/{item.delivery_id}")


async def deliveries(
    session: AsyncSession, *, limit: int, cursor: str | None
) -> tuple[list[DeliveryOut], str | None]:
    """Received deliveries, newest first (host-scoped, outside tenant row-level security)."""
    since, ident = _time_pair(cursor)
    host, _slash, delivery_id = ident.partition("/")
    if since is not None and not (host and delivery_id):
        raise invalid_cursor()
    rows = (
        await session.execute(
            _DELIVERIES,
            {"since": since, "host": host, "delivery_id": delivery_id, "limit": limit + 1},
        )
    ).mappings()
    items = [DeliveryOut(**_delivery_values(row)) for row in rows]
    return _cut(items, limit, _delivery_cursor)


def _payload(body: bytes | None) -> Any | None:  # noqa: ANN401 -- any JSON value
    """The stored body as JSON, or None when it is absent (purged) or not JSON."""
    if body is None:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


async def delivery(
    session: AsyncSession, host: str, delivery_id: str, *, with_payload: bool
) -> DeliveryDetailOut | None:
    """One delivery; the body is read and returned only when `with_payload` (an operator)."""
    row = (
        (
            await session.execute(
                _DELIVERY, {"host": host, "delivery_id": delivery_id, "with_body": with_payload}
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    values = dict(row)
    body = values.pop("body")
    return DeliveryDetailOut(**_delivery_values(values), payload=_payload(body) if with_payload else None)
