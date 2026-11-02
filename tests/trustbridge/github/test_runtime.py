"""`build_host_runtime`: the GitHub side of a process, assembled from the settings."""

from collections.abc import Mapping
from typing import Any

import pytest

from tests.trustbridge.fakes import FakeHostChecker
from tests.trustbridge.helpers import make_settings
from trustbridge.adapters.interface import Tenant
from trustbridge.github.deployments import ArgoTrigger, DeploymentReport
from trustbridge.github.handlers import EventContext
from trustbridge.github.runtime import RuntimeDeps, build_host_runtime
from trustbridge.worker.ports import MemoryTenants
from trustbridge.worker.ports import ReconcileRequests as Scheduler


class Source:
    """A `DeploymentStateSource` that knows nothing."""

    async def observe(self, tenant: Tenant, trigger: ArgoTrigger) -> DeploymentReport | None:
        """Report no state."""
        del tenant, trigger
        return None


def _deps(tenants: MemoryTenants, **extra: Any) -> RuntimeDeps:  # noqa: ANN401
    return RuntimeDeps(
        installations=tenants, installation_ids=tenants, scheduler=Scheduler(), **extra
    )


async def test_the_runtime_wires_monitor_hook_client_and_the_core_handlers(
    key_signers: None,
) -> None:
    """The installation handlers are registered and drop cached tokens through the provider."""
    settings = make_settings()
    tenants = MemoryTenants(lambda _name: settings.github_hosts[0].to_host())
    readied: list[str] = []

    async def on_ready(config: Any) -> None:  # noqa: ANN401
        readied.append(config.name)

    runtime = build_host_runtime(
        settings, _deps(tenants, checker=FakeHostChecker(), on_ready=on_ready)
    )
    await runtime.monitor.check_all()
    assert readied == ["github.com"]
    assert runtime.monitor.is_ready("github.com")
    assert set(runtime.signers) == {"dev"}
    assert runtime.argocd is None
    for event in ("installation", "installation_repositories", "repository"):
        assert runtime.registry.handlers_for(event)
    payload: Mapping[str, Any] = {
        "installation": {"id": 7, "account": {"login": "acme", "id": 1}},
        "repositories": [{"id": 5, "full_name": "acme/web"}],
    }
    ctx = EventContext("github.com", "d1", "installation", "created", payload)
    assert await runtime.registry.dispatch(ctx) == 1
    tenant_id = await tenants.tenant_id_for("github.com", 7)
    assert tenant_id is not None
    assert tenants.repositories[tenant_id] == {5: "acme/web"}


@pytest.mark.usefixtures("key_signers")
def test_a_state_source_enables_the_argocd_handler() -> None:
    """Without a `DeploymentStateSource` there is no Argo CD handler (NG5d supplies it)."""
    settings = make_settings()
    tenants = MemoryTenants(lambda _name: settings.github_hosts[0].to_host())
    assert build_host_runtime(settings, _deps(tenants, state_source=Source())).argocd is not None
