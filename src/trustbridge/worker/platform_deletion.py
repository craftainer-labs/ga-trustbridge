"""The adapter-side port of a tenant deletion's platform step (ADR 0034 step 2; NG6h-5).

`SqlPlatformPort` lists what trustbridge recorded for a tenant (`adapter_credentials` rows that
still hold a credential, `adapter_resources` rows) and removes one item at a time through the
existing cores: `CredentialCore.delete_scope` for a credential and `ResourceCore.delete` for a
resource. Both are write-ahead and run under the adapter lease. Nothing is read from the platform,
so nothing that is not recorded can be removed. The deletion engine's step
(`deletion.platform.AdapterPlatformStep`) calls it; this module imports nothing from `deletion`
(only `deletion.ports`, which holds `PlatformItem`, may be imported by the worker), so it
satisfies the step's port structurally.

A credential is deleted with `deletion_id` (the gate admits that one delete for a disabled tenant,
ADR 0034 B10) and `remove_outputs=False`: the GitHub outputs stay recorded for the GitHub step.
"""

from collections.abc import Sequence
from typing import Protocol, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.adapters.core import CredentialCore
from trustbridge.adapters.models import AdapterCredentialRow, AdapterResourceRow
from trustbridge.adapters.resources import ResourceCore
from trustbridge.adapters.store import CredentialStore, OperationStatus, TenantResolver
from trustbridge.deletion.ports import ItemKind, PlatformItem
from trustbridge.tenancy.context import tenant_context


class SecretRefs(Protocol):
    """The secret store's listing: reference names only, never a value."""

    async def list_refs(self, prefix: str) -> list[str]:
        """The `store:` references whose name starts with `prefix`."""
        ...


RESOURCE_KINDS = frozenset({"webhook", "repository", "project"})
_ADAPTER_NAME = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")


class PlatformDeleteIncompleteError(Exception):
    """A delete did not finish (a manual action is needed, or it is still in flight): retry."""


class UnknownResourceKindError(Exception):
    """A recorded resource key has a kind this step does not know: the deletion is held."""


class SqlPlatformPort:
    """`PlatformPort` over the credential core, the resource core and the recorded rows."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        store: CredentialStore,
        tenants: TenantResolver,
        credentials: CredentialCore,
        resources: ResourceCore,
    ) -> None:
        """Bind the row reads (`sessions`, `store`) and the two cores that do the removing."""
        self._sf = sessions
        self._store = store
        self._tenants = tenants
        self._credentials = credentials
        self._resources = resources

    async def pending(self, tenant_id: UUID) -> Sequence[PlatformItem]:
        """Recorded credentials that still hold one, then recorded resources, from the rows only."""
        items: list[PlatformItem] = []
        with tenant_context(tenant_id):
            async with self._sf() as session:
                adapters = (
                    await session.scalars(
                        select(AdapterCredentialRow.adapter)
                        .where(AdapterCredentialRow.tenant_id == tenant_id)
                        .distinct()
                        .order_by(AdapterCredentialRow.adapter)
                    )
                ).all()
                resources = (
                    await session.execute(
                        select(AdapterResourceRow.adapter, AdapterResourceRow.resource_key)
                        .where(AdapterResourceRow.tenant_id == tenant_id)
                        .order_by(AdapterResourceRow.adapter, AdapterResourceRow.resource_key)
                    )
                ).all()
            for adapter in adapters:
                items.extend(
                    PlatformItem("credential", adapter, row.scope_key)
                    for row in await self._store.list_credentials(tenant_id, adapter)
                    if row.state.current is not None
                )
        for adapter, resource_key in resources:
            kind, _, key = resource_key.partition(":")
            if kind not in RESOURCE_KINDS or not key:
                raise UnknownResourceKindError(f"adapter {adapter}: unrecognised resource kind")
            items.append(PlatformItem(cast("ItemKind", kind), adapter, key))
        return items

    async def remove(self, tenant_id: UUID, deletion_id: UUID, item: PlatformItem) -> None:
        """Delete `item` through its core; raise unless it is gone."""
        tenant = await self._tenants.get_tenant(tenant_id)
        if item.kind != "credential":
            await self._resources.delete(tenant, item.adapter, item.kind, item.key)
            return
        with tenant_context(tenant_id):
            stored = await self._store.get_credential(tenant_id, item.adapter, item.key)
        if stored is None:
            return
        result = await self._credentials.delete_scope(
            tenant, item.adapter, stored.scope, deletion_id=deletion_id, remove_outputs=False
        )
        if result.status not in {None, OperationStatus.FINISHED}:
            raise PlatformDeleteIncompleteError(
                f"adapter {item.adapter}: delete is {result.status.value if result.status else ''}"
            )


class ConnectionNames:
    """`ConnectionNamesPort` over the secret store's listing: adapter names only (ADR 0034).

    Lists `store:tenants/<uuid>/` and returns the last path segment of each direct child. It reads
    no value and returns no reference. A name that is not a plain adapter name is reported as
    `other` (so operator-owned data is still surfaced), and nothing here logs a name.
    """

    def __init__(self, refs: SecretRefs) -> None:
        """List through `refs` (the secret store)."""
        self._refs = refs

    async def names(self, tenant_id: UUID) -> Sequence[str]:
        """The adapters the tenant has connection data for, sorted."""
        prefix = f"tenants/{tenant_id}/"
        found: set[str] = set()
        for ref in await self._refs.list_refs(prefix):
            name = ref.removeprefix(f"store:{prefix}")
            ok = 0 < len(name) <= 64 and set(name) <= _ADAPTER_NAME  # noqa: PLR2004
            found.add(name if ok else "other")
        return sorted(found)
