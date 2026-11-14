"""The platform step of a tenant deletion (ADR 0034 step 2; NG6h-5).

Removes, through the adapters' own delete and revoke operations, every platform-side credential and
resource trustbridge recorded for the tenant, and nothing else. This module knows no adapter: it
works over a `PlatformPort` (the worker's `SqlPlatformPort` in production, which calls
`CredentialCore.delete_scope` and `ResourceCore.delete`, so each removal is write-ahead and under
the adapter lease) and a `ConnectionNamesPort`. It imports nothing from `adapters` (a contract).

One pass works through the recorded items in this order: credentials (tokens, robot accounts,
OIDC trust, repository credentials), then webhooks, then repositories, then projects (NG6h-5b).
An item that fails is skipped for the rest of the pass and the pass moves on, so one stuck item
(a disabled adapter, say) never holds up the ones behind it. A project is never tried while one of
its repositories remains (removed or not, in this pass or before). The pass stops at
`MAX_TRIES_PER_PASS` tries or when the engine's run budget is spent (`ctx.renew` raises
`RunBudgetExhausted`; with failures and no removal that holds the step instead of releasing the
lease, so the attempt is counted). The order above is the order of a first pass (attempts 0).
After held attempts the pass starts at item `attempts % len(items)` of that list and wraps,
trading the kind order for liveness (only the project-after-repository rule is kept), so items
stuck at the head cannot starve the ones behind them across retries; the cursor is never used for
this, because a changed cursor resets the attempts. It reports progress (a changed cursor, which
resets the engine's attempts) only when something was removed; when every remaining item failed it
raises `PlatformStepHeldError` (the engine retries with backoff, counts the attempt and raises the
alert). Repositories and projects hold customer artifacts, so they are removed only when the
request set `delete_platform_resources` (user decision 1); otherwise they stay and are recorded as
skipped (`kept_by_request`). The work list is re-read from the recorded rows on every pass, so a
crash between a removal and the saved cursor converges: a removed item is no longer listed. (The
counts of a pass are saved only when it ends, so a crash can lose that pass's counts: `counts` is
a lower bound.)

The adapters' connection data (`store:tenants/<uuid>/<adapter>`, which includes the per-tenant
Argo CD token) is operator-owned (user decision 4): it is never touched; the last pass records
one `operator_owned_connections` skipped entry per adapter name found, so the operator can remove
them. Only adapter names are listed, never a value and never a ref.

Failures are the engine's: any exception from the port is transient (recorded, backed off, the
deletion held in `platform` and the attempts alert raised). Nothing here ends a deletion `failed`:
an adapter that is down, not enabled, or whose delete needs a manual action blocks the step until
the operator fixes it or acknowledges it with `skip-step` (user decision 2).
"""

import logging
from collections import Counter
from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from trustbridge.deletion.model import (
    LeaseLost,
    NonRetryableError,
    RunBudgetExhausted,
    StepContext,
    StepOutcome,
)
from trustbridge.deletion.ports import ItemKind, PlatformItem

logger = logging.getLogger(__name__)

MAX_TRIES_PER_PASS = 50
"""Bound on removals plus failures tried in one pass (the run budget bounds it in time too)."""
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


class PlatformStepHeldError(Exception):
    """Every remaining item failed in this pass: the engine retries, backs off and alerts.

    The message carries counts and exception class names only, never a message, key or value.
    """


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
        """Remove what can be removed in one pass, or finish once nothing removable is left."""
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
        counts: Counter[str] = Counter()
        failures: Counter[str] = Counter()
        left = set(removable)
        tries = 0
        spent = False
        # Start where the engine's attempt count points, so items stuck at the head cannot keep
        # the run budget from ever reaching the ones behind them. The cursor is not touched: it
        # resets the attempts. A pass with progress resets them to 0 (start at the head again).
        start = record.attempts % len(removable)
        work = [*removable[start:], *removable[:start]]
        for _ in range(2):  # a second sweep takes the projects that waited for a repository
            deferred: list[PlatformItem] = []
            for item in work:
                if tries >= MAX_TRIES_PER_PASS:
                    break
                if _blocked(item, left):
                    deferred.append(item)
                    continue
                try:
                    await ctx.renew()
                    tries += 1
                    removed_it = await self._remove(ctx, item, failures)
                except RunBudgetExhausted:
                    spent = True
                    break
                if removed_it:
                    left.discard(item)
                    counts[COUNT_CATEGORY[item.kind]] += 1
            if spent or not deferred or not counts:
                break
            work = deferred
        if not counts:
            if spent and not failures:
                raise RunBudgetExhausted
            # Held, also when the budget went on failures: the attempt is counted, which rotates
            # the next start, where releasing the lease would retry the same head items.
            raise _held(failures, len(removable))
        removed = int(record.step_cursor.get("removed", 0)) + sum(counts.values())
        return StepOutcome(counts=dict(counts), cursor={"removed": removed}, done=False)

    async def _remove(self, ctx: StepContext, item: PlatformItem, failures: Counter[str]) -> bool:
        """Remove one item; False (counted, logged by class only) when it failed."""
        record = ctx.record
        try:
            await self._platform.remove(record.tenant_id, record.id, item)
        except LeaseLost, RunBudgetExhausted, NonRetryableError:
            raise
        except Exception as error:
            failures[type(error).__name__] += 1
            logger.warning(
                "tenant deletion %s: platform %s removal failed (%s), skipping it this pass",
                record.id,
                item.kind,
                type(error).__name__,
            )
            return False
        return True


def _held(failures: Counter[str], total: int) -> PlatformStepHeldError:
    kinds = ", ".join(f"{name} x{n}" for name, n in sorted(failures.items()))
    return PlatformStepHeldError(
        f"{sum(failures.values())} of {total} removable items failed ({kinds})"
    )


def _blocked(item: PlatformItem, left: set[PlatformItem]) -> bool:
    """A project is never tried while one of its repositories (`<project>/<repo>`) remains."""
    if item.kind != "project":
        return False
    prefix = f"{item.key}/"
    return any(
        other.kind == "repository"
        and other.adapter == item.adapter
        and other.key.startswith(prefix)
        for other in left
    )
