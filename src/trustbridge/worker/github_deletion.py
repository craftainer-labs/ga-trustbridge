"""The GitHub-side port of a tenant deletion's GitHub step (ADR 0034 step 3; NG6h-6).

`SqlGithubPort` lists what trustbridge recorded for a tenant (the `outputs` of its
`adapter_credentials` rows and its `github_environments` rows), and removes one item at a time: a
secret or variable through `GitHubOutputWriter.remove` (absent is fine), an environment through the
tenant gateway (`DELETE`, 404 is fine), each with an installation token down-scoped to the one
repository and permission. Nothing is read from GitHub except an environment's `created_at` and
whether the installation exists, so nothing that is not recorded can be removed. The deletion
engine's step (`deletion.github.GithubDeletionStep`) calls it; this module imports nothing from
`deletion` (only `deletion.ports`, which holds the item types), so it satisfies the step's port
structurally.

The credential row's entry for a removed output is cleared right after the GitHub call, so a
crash between the two repeats an idempotent delete and the step converges. The tenant is disabled
and drained when this runs, so no writer races the row update (it still locks the row).
"""

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.adapters import codec
from trustbridge.adapters.models import AdapterCredentialRow
from trustbridge.adapters.store import DeliveredOutput, OutputError, OutputWriter, TenantResolver
from trustbridge.deletion.ports import GithubOutput, TrackedEnvironment, TrackedEnvironments
from trustbridge.github.client import HTTP_NOT_FOUND, GitHubApiError
from trustbridge.github.gateway import Call, TenantGateway, enc
from trustbridge.github.installations import InstallationInfo
from trustbridge.tenancy.context import tenant_context


class InstallationReader(Protocol):
    """Reads an installation from GitHub with the App JWT (`AppInstallationLookup`)."""

    async def fetch(self, host: str, installation_id: int) -> InstallationInfo | None:
        """The installation, or None when GitHub answers 404 (it is gone)."""
        ...


class SqlGithubPort:
    """`GithubPort` over the credential rows, the environment store, the writer and the gateway."""

    def __init__(  # noqa: PLR0913 -- the port's collaborators, keyword-only
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        tenants: TenantResolver,
        writer: OutputWriter,
        gateway: TenantGateway,
        environments: TrackedEnvironments,
        installations: InstallationReader,
    ) -> None:
        """Bind the row reads (`sessions`, `environments`) and the GitHub side."""
        self._sf = sessions
        self._tenants = tenants
        self._writer = writer
        self._gateway = gateway
        self._environments = environments
        self._installations = installations

    async def installation_exists(self, host: str, installation_id: int) -> bool:
        """Whether GitHub still has the installation (a suspended one exists)."""
        return await self._installations.fetch(host, installation_id) is not None

    async def outputs(self, tenant_id: UUID) -> Sequence[GithubOutput]:
        """The secrets and variables recorded in the tenant's credential rows, from the rows."""
        found: list[GithubOutput] = []
        for adapter, scope_key, outputs in await self._rows(tenant_id):
            for o in outputs:
                if o.kind != "environment":
                    found.append(
                        GithubOutput(
                            adapter, scope_key, o.kind, o.name, o.repository_id, o.environment
                        )
                    )
        return found

    async def remove_output(self, tenant_id: UUID, output: GithubOutput) -> bool:
        """Delete it from GitHub, then clear it from its credential row.

        False when the repository itself answered 404 (the entry is cleared anyway).
        """
        tenant = await self._tenants.get_tenant(tenant_id)
        delivered = DeliveredOutput(
            output.kind, output.name, output.repository_id, output.environment
        )
        try:
            await self._writer.remove(tenant, [delivered])
        except OutputError as exc:
            # The writer already treats an absent secret as removed (`from exc` keeps the cause);
            # a 404 that gets here is the repository itself (deleted, transferred or no longer
            # visible): nothing can be removed from our side, so the entry is cleared and the
            # step reports it as unreachable rather than removed.
            cause = exc.__cause__
            if not (isinstance(cause, GitHubApiError) and cause.status == HTTP_NOT_FOUND):
                raise
            await self._clear(tenant_id, output.adapter, output.scope_key, lambda o: o == delivered)
            return False
        await self._clear(tenant_id, output.adapter, output.scope_key, lambda o: o == delivered)
        return True

    async def environments(self, tenant_id: UUID) -> Sequence[TrackedEnvironment]:
        """The tracked environments with the time their rows were written."""
        return [
            TrackedEnvironment(repository_id, name, created_at)
            for repository_id, name, created_at in await self._environments.tracked_since(tenant_id)
        ]

    async def untracked_environments(self, tenant_id: UUID) -> int:
        """Distinct environments the credential rows name that no tracking row covers."""
        tracked = set(await self._environments.tracked(tenant_id))
        named = {
            (o.repository_id, o.name)
            for _, _, outputs in await self._rows(tenant_id)
            for o in outputs
            if o.kind == "environment"
        }
        return len(named - tracked)

    async def environment_created_at(
        self, tenant_id: UUID, environment: TrackedEnvironment
    ) -> datetime | None:
        """GitHub's `created_at` for the environment; None when the repository or it is gone."""
        tenant = await self._tenants.get_tenant(tenant_id)
        try:
            full = await self._gateway.full_name(tenant, environment.repository_id)
            resp = await self._gateway.call(
                tenant,
                Call(
                    "GET",
                    f"/repos/{full}/environments/{enc(environment.name)}",
                    {"administration": "read"},
                    [environment.repository_id],
                ),
            )
        except GitHubApiError as exc:
            if exc.status == HTTP_NOT_FOUND:
                return None
            raise
        return datetime.fromisoformat(str(resp.data["created_at"]))

    async def delete_environment(self, tenant_id: UUID, environment: TrackedEnvironment) -> None:
        """`DELETE` the environment; a 404 (already gone) is fine."""
        tenant = await self._tenants.get_tenant(tenant_id)
        try:
            full = await self._gateway.full_name(tenant, environment.repository_id)
            await self._gateway.call(
                tenant,
                Call(
                    "DELETE",
                    f"/repos/{full}/environments/{enc(environment.name)}",
                    {"administration": "write"},
                    [environment.repository_id],
                ),
            )
        except GitHubApiError as exc:
            if exc.status != HTTP_NOT_FOUND:
                raise

    async def release_environment(self, tenant_id: UUID, environment: TrackedEnvironment) -> None:
        """Clear what the credential rows record for the environment, then forget its row.

        That is its `environment` outputs and the secrets and variables scoped to it (they go
        with the environment, or belong to a person's recreation of it and are not ours to
        delete by name). Only rows with such an entry are locked.
        """

        def covered(o: DeliveredOutput) -> bool:
            if o.repository_id != environment.repository_id:
                return False
            return o.environment == environment.name or (
                o.kind == "environment" and o.name == environment.name
            )

        for adapter, scope_key, outputs in await self._rows(tenant_id):
            if any(covered(o) for o in outputs):
                await self._clear(tenant_id, adapter, scope_key, covered)
        await self._environments.forget(tenant_id, environment.repository_id, environment.name)

    async def _rows(self, tenant_id: UUID) -> list[tuple[str, str, tuple[DeliveredOutput, ...]]]:
        """`(adapter, scope_key, outputs)` of each credential row that records outputs."""
        query = (
            select(
                AdapterCredentialRow.adapter,
                AdapterCredentialRow.scope_key,
                AdapterCredentialRow.outputs,
            )
            .where(AdapterCredentialRow.tenant_id == tenant_id)
            .order_by(AdapterCredentialRow.adapter, AdapterCredentialRow.scope_key)
        )
        with tenant_context(tenant_id):
            async with self._sf() as session:
                rows = (await session.execute(query)).all()
        return [
            (row.adapter, row.scope_key, codec.outputs_from_json([dict(o) for o in row.outputs]))
            for row in rows
            if row.outputs
        ]

    async def _clear(
        self,
        tenant_id: UUID,
        adapter: str,
        scope_key: str,
        drop: Callable[[DeliveredOutput], bool],
    ) -> None:
        """Remove the entries `drop` selects from one credential row (locked while it changes)."""
        query = (
            select(AdapterCredentialRow)
            .where(
                AdapterCredentialRow.tenant_id == tenant_id,
                AdapterCredentialRow.adapter == adapter,
                AdapterCredentialRow.scope_key == scope_key,
            )
            .with_for_update()
        )
        with tenant_context(tenant_id):
            async with self._sf() as session, session.begin():
                row = await session.scalar(query)
                if row is None:
                    return
                current = codec.outputs_from_json([dict(o) for o in row.outputs])
                kept = tuple(o for o in current if not drop(o))
                if len(kept) != len(current):
                    row.outputs = codec.outputs_to_json(kept)
