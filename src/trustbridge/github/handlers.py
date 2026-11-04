"""Handler registry for inbound GitHub events, and the GitHub core's lifecycle handlers.

NG3's worker loads a stored delivery, resolves its host, builds an `EventContext` and calls
`HandlerRegistry.dispatch`. Handlers are idempotent (reconcile style): the same delivery may be
handled twice. A handler that raises makes the worker record a failure and retry.

The core registers handlers for `installation`, `installation_repositories` and `repository`
(`register_core_handlers`). `new_permissions_accepted` is an `installation` action. GitHub
documents no webhook event for an environment's protection rules changing, so the OIDC
eligibility re-evaluation (ADR 0029 A12) runs at each reconcile, scheduled after these events.
"""

import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from trustbridge.github.installations import InstallationInfo
from trustbridge.github.ports import InstallationStore, ReconcileScheduler

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EventContext:
    """One verified delivery, as the worker hands it to handlers."""

    host: str
    delivery_id: str
    event: str
    action: str | None
    payload: Mapping[str, Any]


Handler = Callable[[EventContext], Awaitable[None]]


class HandlerRegistry:
    """Maps GitHub event names to handlers; `dispatch` runs them in registration order."""

    def __init__(self) -> None:
        """Start empty."""
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def register(self, event: str, handler: Handler) -> None:
        """Register `handler` for `event` (the `X-GitHub-Event` name)."""
        self._handlers[event].append(handler)

    def handlers_for(self, event: str) -> Sequence[Handler]:
        """Return the handlers registered for `event`."""
        return tuple(self._handlers.get(event, ()))

    async def dispatch(self, ctx: EventContext) -> int:
        """Run every handler for the event; return how many ran (0 means the event is ignored)."""
        handlers = self.handlers_for(ctx.event)
        for handler in handlers:
            await handler(ctx)
        return len(handlers)


class InstallationLookup(Protocol):
    """Reads an installation's current state from GitHub (`installations.AppInstallationLookup`)."""

    async def fetch(self, host: str, installation_id: int) -> InstallationInfo | None:
        """The installation's state, or None when it no longer exists."""
        ...


class TenantAudit(Protocol):
    """Records the tenant state an installation sync left behind (ADR 0032 `tenant.sync`)."""

    async def tenant_synced(
        self, *, tenant_id: UUID, state: str, delivery_id: str, sender_id: int | None
    ) -> None:
        """Write the event. A failure propagates, so the delivery is retried."""
        ...


class InstallationHandlers:
    """Keeps tenants and their repositories in step with installation events.

    GitHub does not order webhook deliveries (see `installations.py` for the documentation
    checked), so `installation` `created`, `deleted`, `suspend` and `unsuspend` are only
    triggers: the state to apply is read from the API at handling time. A retried `created`
    after a later `deleted` therefore finds the installation gone and resurrects nothing.
    """

    def __init__(
        self,
        store: InstallationStore,
        scheduler: ReconcileScheduler,
        lookup: InstallationLookup,
        on_installation_changed: Callable[[str, int], None] | None = None,
        audit: TenantAudit | None = None,
    ) -> None:
        """`on_installation_changed(host, installation_id)` drops cached installation tokens.

        `audit`, when given, records a `tenant.sync` event after each applied sync.
        """
        self._store = store
        self._scheduler = scheduler
        self._lookup = lookup
        self._changed = on_installation_changed
        self._audit = audit

    @staticmethod
    def _installation(payload: Mapping[str, Any]) -> tuple[int, str, int]:
        inst = payload["installation"]
        account = inst.get("account") or {}
        return int(inst["id"]), str(account.get("login", "")), int(account.get("id", 0))

    @staticmethod
    def _repos(items: Sequence[Mapping[str, Any]] | None) -> list[tuple[int, str]]:
        return [(int(r["id"]), str(r["full_name"])) for r in items or ()]

    async def on_installation(self, ctx: EventContext) -> None:
        """Handle `installation`: created, deleted, suspend, unsuspend, new_permissions_accepted."""
        installation_id, login, account_id = self._installation(ctx.payload)
        action = ctx.action
        if action in {"created", "deleted", "suspend", "unsuspend"}:
            await self._sync(ctx, installation_id, login, account_id)
            return
        if action == "new_permissions_accepted":
            self._notify(ctx.host, installation_id)
            found = await self._store.tenant_id_for(ctx.host, installation_id)
            if found is not None:
                await self._scheduler.schedule(found)
            return
        _log.info("ignoring installation action %s", action)

    async def _sync(self, ctx: EventContext, installation_id: int, login: str, acct: int) -> None:
        """Apply the installation's state as GitHub reports it now, whatever the event was."""
        info = await self._lookup.fetch(ctx.host, installation_id)
        if info is None:  # gone: never create or re-activate a tenant for it
            gone = await self._store.set_installation_state(ctx.host, installation_id, "deleted")
            if gone is not None:
                await self._record(ctx, gone, "deleted")
            self._notify(ctx.host, installation_id)
            return
        tenant_id = await self._store.upsert_installation(
            ctx.host,
            installation_id,
            info.login or login,
            info.account_id or acct,
            suspended=info.suspended,
        )
        if ctx.action == "created":
            await self._store.apply_repository_changes(
                tenant_id, self._repos(ctx.payload.get("repositories")), []
            )
        await self._record(ctx, tenant_id, "suspended" if info.suspended else "active")
        self._notify(ctx.host, installation_id)
        if not info.suspended:
            await self._scheduler.schedule(tenant_id)

    async def _record(self, ctx: EventContext, tenant_id: UUID, state: str) -> None:
        """Audit the state this sync applied (`sender` is the GitHub user's numeric id)."""
        if self._audit is None:
            return
        sender = ctx.payload.get("sender") or {}
        sender_id = sender.get("id")
        await self._audit.tenant_synced(
            tenant_id=tenant_id,
            state=state,
            delivery_id=ctx.delivery_id,
            sender_id=sender_id if isinstance(sender_id, int) else None,
        )

    def _notify(self, host: str, installation_id: int) -> None:
        if self._changed is not None:
            self._changed(host, installation_id)

    async def on_installation_repositories(self, ctx: EventContext) -> None:
        """Handle `installation_repositories`: repositories added to or removed from the install."""
        installation_id, _login, _account = self._installation(ctx.payload)
        tenant_id = await self._store.tenant_id_for(ctx.host, installation_id)
        if tenant_id is None:
            return
        added = self._repos(ctx.payload.get("repositories_added"))
        removed = [int(r["id"]) for r in ctx.payload.get("repositories_removed") or ()]
        await self._store.apply_repository_changes(tenant_id, added, removed)
        await self._scheduler.schedule(tenant_id)

    async def on_repository(self, ctx: EventContext) -> None:
        """Handle `repository`: renamed, transferred, deleted, archived (identity is the id)."""
        installation = ctx.payload.get("installation")
        if not installation:
            return
        tenant_id = await self._store.tenant_id_for(ctx.host, int(installation["id"]))
        if tenant_id is None:
            return
        repo = ctx.payload["repository"]
        repository_id = int(repo["id"])
        if ctx.action in {"renamed", "transferred"}:
            await self._store.rename_repository(tenant_id, repository_id, str(repo["full_name"]))
        elif ctx.action == "deleted":
            await self._store.apply_repository_changes(tenant_id, [], [repository_id])
        elif ctx.action not in {"archived", "unarchived", "privatized", "publicized", "edited"}:
            return
        await self._scheduler.schedule(tenant_id)


def register_core_handlers(registry: HandlerRegistry, handlers: InstallationHandlers) -> None:
    """Register the installation lifecycle handlers under their GitHub event names."""
    registry.register("installation", handlers.on_installation)
    registry.register("installation_repositories", handlers.on_installation_repositories)
    registry.register("repository", handlers.on_repository)
