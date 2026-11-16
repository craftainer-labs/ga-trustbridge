"""The writes behind the tenant-deletion routes (ADR 0034, NG6h-7b).

Each function runs in the caller's transaction and calls one `SECURITY DEFINER` function of the
database (the app role cannot write `tenant_deletions` any other way): `tb_request_tenant_deletion`
(NG6h-1), `tb_cancel_tenant_deletion` and `tb_operator_fail_tenant_deletion` (NG6h-4a-1) and
`tb_skip_tenant_deletion_step` (NG6h-7b). A refusal is an `AdminProblem` (404 or 409), so the route
rolls the change back and records a `failure` event; the audit events themselves are the route's.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from trustbridge.admin import queries
from trustbridge.admin.problems import AdminProblem
from trustbridge.admin.schemas import DeletionRequestIn, TenantDeletionOut
from trustbridge.audit import project
from trustbridge.rbac import Principal

_REQUEST = text(
    "SELECT * FROM tb_request_tenant_deletion("
    ":tenant, :host, :installation, :org, :sub, :iss, :platform, :not_before)"
)
_CANCEL = text("SELECT tb_cancel_tenant_deletion(:id)")
_SKIP = text("SELECT * FROM tb_skip_tenant_deletion_step(:id, :actor)")
_FAIL = text("SELECT tb_operator_fail_tenant_deletion(:id, :actor, :reason)")
LEASE_HELD = "TB003"
TERMINAL_OR_INVALID = "TB004"
NOT_FOUND = "Deletion not found"
SKIP_REFUSALS = {
    "terminal": "The deletion has finished or ended",
    "not_skippable": "Only the platform, github or database step can be skipped",
    "not_failed": "The step has not failed yet",
    "lease_held": "The deletion is being worked on; try again when its lease is free",
}
"""The 409 detail of each `tb_skip_tenant_deletion_step` outcome that is not a skip."""


def not_before(grace_seconds: int, *, now: datetime | None = None) -> datetime | None:
    """When the grace window ends (None for no window): the server's own, never the caller's."""
    if grace_seconds <= 0:
        return None
    return (now or datetime.now(UTC)) + timedelta(seconds=grace_seconds)


def state_of(record: TenantDeletionOut, **extra: Any) -> dict[str, Any]:  # noqa: ANN401 -- scalars
    """The `before` or `after` of a deletion event: ids, the flag, the state and `extra`."""
    return project(
        "deletion",
        {
            "deletion_id": record.id,
            "host": record.host,
            "installation_id": record.installation_id,
            "delete_platform_resources": record.delete_platform_resources,
            "not_before": record.not_before,
            **extra,
        },
    )


def transition(
    deletion_id: UUID,
    from_state: str,
    to_state: str,
    **extra: Any,  # noqa: ANN401 -- scalars
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The `(before, after)` of an operator's change of state (the engine's `before` shape)."""
    before = project("deletion", {"deletion_id": deletion_id, "from_state": from_state})
    after = project(
        "deletion",
        {"deletion_id": deletion_id, "from_state": from_state, "to_state": to_state, **extra},
    )
    return before, after


async def request(
    session: AsyncSession,
    tenant_id: UUID,
    body: DeletionRequestIn,
    principal: Principal,
    grace_seconds: int,
) -> tuple[TenantDeletionOut, bool]:
    """Request the tenant's deletion, or return the open one; `(record, created)`.

    The function checks, under the tenant row lock, that the tenant is admin-disabled and that
    the typed confirmation matches; it does not store the confirmation.
    """
    row = (
        await session.execute(
            _REQUEST,
            {
                "tenant": tenant_id,
                "host": body.host,
                "installation": body.installation_id,
                "org": body.organization,
                "sub": principal.sub,
                "iss": principal.iss or "",
                "platform": body.delete_platform_resources,
                "not_before": not_before(grace_seconds),
            },
        )
    ).one()
    if row.outcome == "not_found":
        raise AdminProblem(404, "not-found", "Tenant not found")
    if row.outcome == "not_disabled":
        raise AdminProblem(409, "conflict", "The tenant must be disabled before it is deleted")
    if row.outcome == "confirmation_mismatch":
        raise AdminProblem(409, "conflict", "The confirmation does not match the tenant")
    record = await queries.tenant_deletion(session, row.deletion_id)
    if record is None:  # the row was written a moment ago in this transaction: unreachable
        raise AdminProblem(404, "not-found", NOT_FOUND)
    return record, row.outcome == "created"


async def cancel(session: AsyncSession, deletion_id: UUID) -> None:
    """Cancel a deletion that has not started its first destructive step (idempotent)."""
    outcome = (await session.execute(_CANCEL, {"id": deletion_id})).scalar_one()
    if outcome == "not_found":
        raise AdminProblem(404, "not-found", NOT_FOUND)
    if outcome == "not_cancellable":
        raise AdminProblem(
            409, "conflict", "A deletion can be cancelled only before its first step"
        )


async def skip_step(session: AsyncSession, deletion_id: UUID, actor: str) -> tuple[str, str]:
    """Acknowledge and skip the failing step; returns `(from_state, to_state)`."""
    row = (await session.execute(_SKIP, {"id": deletion_id, "actor": actor})).one()
    if row.outcome == "not_found":
        raise AdminProblem(404, "not-found", NOT_FOUND)
    if row.outcome != "skipped":
        raise AdminProblem(409, "conflict", SKIP_REFUSALS[row.outcome])
    return row.from_state, row.to_state


async def fail(session: AsyncSession, deletion_id: UUID, actor: str, reason: str) -> None:
    """End a stuck deletion as `failed`, with no lease of its own (refused while one is live)."""
    try:
        await session.execute(_FAIL, {"id": deletion_id, "actor": actor, "reason": reason})
    except DBAPIError as error:
        code = getattr(error.orig, "sqlstate", None)
        if code == LEASE_HELD:
            raise AdminProblem(
                409, "conflict", "The deletion is being worked on; try again when its lease is free"
            ) from None
        if code == TERMINAL_OR_INVALID:
            raise AdminProblem(
                409, "conflict", "The deletion has finished, ended, or can only finish"
            ) from None
        raise
