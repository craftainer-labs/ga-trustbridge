"""The platform step of a tenant deletion (ADR 0034 step 2; NG6h-5).

Removes, through the adapters' own delete and revoke operations, every platform-side credential and
resource trustbridge recorded for the tenant, and nothing else. This module knows no adapter: it
works over a `PlatformPort` (the worker's `SqlPlatformPort` in production, which calls
`CredentialCore.delete_scope` and `ResourceCore.delete`, so each removal is write-ahead and under
the adapter lease) and a `ConnectionNamesPort`. It imports nothing from `adapters` (a contract).

One pass removes one item, in this order: credentials (tokens, robot accounts, OIDC trust,
repository credentials), then webhooks, then repositories, then projects. Repositories and
projects hold customer artifacts, so they are removed only when the request set
`delete_platform_resources` (user decision 1); otherwise they stay and are recorded as skipped
(`kept_by_request`). The work list is re-read from the recorded rows on every pass, so a crash
between a removal and the saved cursor converges: a removed item is no longer listed. (The count
of that one item can be missed, which makes `counts` a lower bound after a crash.)

The adapters' connection data (`store:tenants/<uuid>/<adapter>`, which includes the per-tenant
Argo CD token) is operator-owned (user decision 4): it is never touched; the last pass records
one `operator_owned_connections` skipped entry per adapter name found, so the operator can remove
them. Only adapter names are listed, never a value and never a ref.

Failures are the engine's: any exception from the port is transient (recorded, backed off, the
deletion held in `platform` and the attempts alert raised). Nothing here ends a deletion `failed`:
an adapter that is down, not enabled, or whose delete needs a manual action blocks the step until
the operator fixes it or acknowledges it with `skip-step` (user decision 2).
"""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from trustbridge.deletion.model import StepContext, StepOutcome
from trustbridge.deletion.ports import ItemKind, PlatformItem

ORDER: tuple[ItemKind, ...] = ("credential", "webhook", "repository", "project")
"""The removal order (NG6h-5): credentials, then webhooks, repositories, projects."""
BY_REQUEST: frozenset[ItemKind] = frozenset({"repository", "project"})
"""Kinds that hold customer artifacts: removed only with `delete_platform_resources`."""

COUNT_CATEGORY: dict[ItemKind, str] = {
    "credential": "platform_access_grants",
    "webhook": "platform_webhooks",
    "repository": "platform_repositories",
    "project": "platform_projects",
}
"""Names in `counts` and `skipped`; they avoid the admin view's secret words (`credential`)."""
KEPT_REASON = "kept_by_request"
OPERATOR_OWNED_CATEGORY = "operator_owned_connections"
OPERATOR_OWNED_REASON = "operator_owned"


class PlatformPort(Protocol):
    """The recorded platform state of a tenant, and the adapter calls that remove it."""

    async def pending(self, tenant_id: UUID) -> Sequence[PlatformItem]:
        """The items still recorded for the tenant, from the rows only (no platform call)."""
        ...

    async def remove(self, tenant_id: UUID, deletion_id: UUID, item: PlatformItem) -> None:
        """Remove one item through the adapter's delete or revoke, under the deletion's origin.

        Returns only once the item is gone from the platform and from the recorded state; raises
        for anything else (the platform unreachable, the adapter not enabled, a delete that needs
        a manual action), which the engine retries.
        """
        ...


class ConnectionNamesPort(Protocol):
    """Which adapters have operator-provisioned connection data for the tenant (names only)."""

    async def names(self, tenant_id: UUID) -> Sequence[str]:
        """Adapter names, sorted; never a value or a reference."""
        ...


def _skipped(
    pending: Sequence[PlatformItem], connections: Sequence[str]
) -> list[dict[str, object]]:
    kept = [
        {
            "category": COUNT_CATEGORY[kind],
            "reason": KEPT_REASON,
            "count": sum(1 for item in pending if item.kind == kind),
        }
        for kind in ORDER
        if kind in BY_REQUEST and any(item.kind == kind for item in pending)
    ]
    owned = [
        {
            "category": OPERATOR_OWNED_CATEGORY,
            "reason": OPERATOR_OWNED_REASON,
            "adapter": name,
            "count": 1,
        }
        for name in connections
    ]
    return [*kept, *owned]


class AdapterPlatformStep:
    """The `PlatformStep` over a `PlatformPort` and a `ConnectionNamesPort`."""

    def __init__(self, platform: PlatformPort, connections: ConnectionNamesPort) -> None:
        """Remove through `platform`; list operator-owned connection data through `connections`."""
        self._platform = platform
        self._connections = connections

    async def run(self, ctx: StepContext) -> StepOutcome:
        """Remove the next item (one pass), or finish once nothing removable is left."""
        record = ctx.record
        pending = await self._platform.pending(record.tenant_id)
        removable = [
            item
            for kind in ORDER
            if kind not in BY_REQUEST or record.delete_platform_resources
            for item in pending
            if item.kind == kind
        ]
        if not removable:
            connections = await self._connections.names(record.tenant_id)
            return StepOutcome(skipped=_skipped(pending, connections))
        item = removable[0]
        await ctx.renew()
        await self._platform.remove(record.tenant_id, record.id, item)
        removed = int(record.step_cursor.get("removed", 0)) + 1
        return StepOutcome(
            counts={COUNT_CATEGORY[item.kind]: 1},
            cursor={"removed": removed},
            done=False,
        )
