"""Postgres store of the environments trustbridge created (ADR 0034 decision B6; NG6h-6a).

One row per `(tenant, repository, environment)` that the GitHub writer created itself. Every call
runs in the tenant's context (ADR 0031), so the `tenant_isolation` policy applies once RLS is
enforced. The same class is the writer's `EnvironmentTracker` and, structurally, the deletion's
`TrackedEnvironments` port (`trustbridge.deletion.ports`; the packages do not import each other).
"""

from collections.abc import Callable, Sequence
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from trustbridge.github.models import GitHubEnvironmentRow
from trustbridge.tenancy.context import tenant_context


class SqlEnvironmentStore:
    """`EnvironmentTracker` and `TrackedEnvironments` over `github_environments`."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        """Use `session_factory` for a short-lived session per call."""
        self._sf = session_factory

    async def record_created(
        self, tenant_id: UUID, host: str, repository_id: int, name: str
    ) -> None:
        """Insert the row; a repeat (the environment was created again) refreshes `created_at`."""
        stmt = insert(GitHubEnvironmentRow).values(
            tenant_id=tenant_id, host=host, repository_id=repository_id, environment_name=name
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "repository_id", "environment_name"],
            set_={"created_at": func.now()},
        )
        with tenant_context(tenant_id):
            async with self._sf() as session, session.begin():
                await session.execute(stmt)

    async def tracked(self, tenant_id: UUID) -> Sequence[tuple[int, str]]:
        """The tenant's tracked environments as `(repository_id, name)`, in key order."""
        query = (
            select(GitHubEnvironmentRow.repository_id, GitHubEnvironmentRow.environment_name)
            .where(GitHubEnvironmentRow.tenant_id == tenant_id)
            .order_by(GitHubEnvironmentRow.repository_id, GitHubEnvironmentRow.environment_name)
        )
        with tenant_context(tenant_id):
            async with self._sf() as session:
                return [
                    (row.repository_id, row.environment_name)
                    for row in await session.execute(query)
                ]

    async def forget(self, tenant_id: UUID, repository_id: int, name: str) -> None:
        """Remove one row once its environment is deleted (absent is fine)."""
        stmt = delete(GitHubEnvironmentRow).where(
            GitHubEnvironmentRow.tenant_id == tenant_id,
            GitHubEnvironmentRow.repository_id == repository_id,
            GitHubEnvironmentRow.environment_name == name,
        )
        with tenant_context(tenant_id):
            async with self._sf() as session, session.begin():
                await session.execute(stmt)
