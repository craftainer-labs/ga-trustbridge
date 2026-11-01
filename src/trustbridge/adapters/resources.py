"""Non-secret resource operations (projects, repositories, webhooks)."""

import asyncio
from collections.abc import Awaitable

from trustbridge.adapters.core import CoreDeps
from trustbridge.adapters.interface import (
    AdapterError,
    Applied,
    ProjectRef,
    ProjectSpec,
    RepoSpec,
    ResourceState,
    Tenant,
    WebhookTarget,
    resource_name,
)
from trustbridge.adapters.store import CoreSettings


class ResourceCore:
    """Runs `ensure_project`, `ensure_repository`, `ensure_webhook` and `delete` under the lease."""

    def __init__(self, deps: CoreDeps, settings: CoreSettings | None = None) -> None:
        """Keep the dependencies and settings."""
        self._d = deps
        self._s = settings or CoreSettings()

    async def ensure_project(self, tenant: Tenant, adapter: str, spec: ProjectSpec) -> Applied:
        """Converge a project; the core chooses its deterministic platform name."""
        key = f"project:{spec.key}"
        bound = await self._d.registry.open(tenant, adapter)
        async with self._d.leases.hold(tenant_id=tenant.id, adapter=adapter, key=key) as lease:
            prior = await self._prior(tenant, adapter, "project", spec.key)
            applied = await self._bounded(bound.adapter.ensure_project(tenant, spec, prior))
            await self._d.store.put_resource(lease, tenant.id, adapter, key, applied.state)
            return applied

    async def ensure_repository(
        self, tenant: Tenant, adapter: str, project_key: str, spec: RepoSpec
    ) -> Applied:
        """Converge a repository inside an existing project."""
        key = f"repository:{project_key}/{spec.key}"
        bound = await self._d.registry.open(tenant, adapter)
        async with self._d.leases.hold(tenant_id=tenant.id, adapter=adapter, key=key) as lease:
            project = await self._d.store.get_resource(tenant.id, adapter, f"project:{project_key}")
            if project is None:
                msg = f"project {project_key!r} has not been created"
                raise AdapterError(False, msg)
            prior = await self._prior(tenant, adapter, "repository", f"{project_key}/{spec.key}")
            applied = await self._bounded(
                bound.adapter.ensure_repository(ProjectRef(tenant, project), spec, prior)
            )
            await self._d.store.put_resource(lease, tenant.id, adapter, key, applied.state)
            return applied

    async def ensure_webhook(
        self, tenant: Tenant, adapter: str, key: str, target: WebhookTarget
    ) -> Applied:
        """Converge a platform webhook that calls back into trustbridge."""
        rkey = f"webhook:{key}"
        bound = await self._d.registry.open(tenant, adapter)
        async with self._d.leases.hold(tenant_id=tenant.id, adapter=adapter, key=rkey) as lease:
            prior = await self._prior(tenant, adapter, "webhook", key)
            applied = await self._bounded(bound.adapter.ensure_webhook(target, prior))
            await self._d.store.put_resource(lease, tenant.id, adapter, rkey, applied.state)
            return applied

    async def delete(self, tenant: Tenant, adapter: str, kind: str, key: str) -> Applied | None:
        """Delete a resource and forget its state; None when it was never created."""
        rkey = f"{kind}:{key}"
        bound = await self._d.registry.open(tenant, adapter)
        async with self._d.leases.hold(tenant_id=tenant.id, adapter=adapter, key=rkey) as lease:
            prior = await self._d.store.get_resource(tenant.id, adapter, rkey)
            if prior is None:
                return None
            applied = await self._bounded(bound.adapter.delete(prior))
            await self._d.store.put_resource(lease, tenant.id, adapter, rkey, None)
            return applied

    async def _prior(self, tenant: Tenant, adapter: str, kind: str, key: str) -> ResourceState:
        stored = await self._d.store.get_resource(tenant.id, adapter, f"{kind}:{key}")
        return stored or ResourceState(name=resource_name(tenant.id, kind, key), ref={})

    async def _bounded(self, awaitable: Awaitable[Applied]) -> Applied:
        try:
            async with asyncio.timeout(self._s.adapter_call_timeout_seconds):
                return await awaitable
        except TimeoutError as exc:
            raise AdapterError(True, "adapter call timed out") from exc
