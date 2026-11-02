"""Assembles the GitHub side of a trustbridge process: `build_host_runtime(settings, deps)`.

One `HostRuntime` per process (api, worker, reconciler): the signers built from `SIGNERS`, the
`HostMonitor` (per-host availability, rechecked by `monitor.start`), the installation token
provider and gateway, the hook-delivery client the reconciler uses, the `HandlerRegistry` the
worker dispatches through (`register_core_handlers`), and the Argo CD notification handler when
a `DeploymentStateSource` is supplied (NG5d provides it; without one the notification route
answers 503). The processes pass the ports implemented over NG3's components in `RuntimeDeps`.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from trustbridge.github.availability import HostChecker, HostMonitor, MonitorDeps, RegistrationStore
from trustbridge.github.client import GitHubRequester
from trustbridge.github.deployments import (
    ArgoCdDeploymentHandler,
    DeploymentReporter,
    DeploymentStateSource,
)
from trustbridge.github.gateway import GatewayDeps, TenantGateway
from trustbridge.github.handlers import (
    HandlerRegistry,
    InstallationHandlers,
    register_core_handlers,
)
from trustbridge.github.hook_deliveries import GitHubHookDeliveries
from trustbridge.github.hosts import GitHubHostConfig
from trustbridge.github.ports import InstallationResolver, InstallationStore, ReconcileScheduler
from trustbridge.github.tokens import InstallationTokenProvider
from trustbridge.settings import TrustbridgeSettings
from trustbridge.signer.base import AppKeySigner
from trustbridge.signer.factory import build_signers


@dataclass(frozen=True, slots=True)
class RuntimeDeps:
    """What a process supplies to `build_host_runtime`."""

    installations: InstallationStore
    installation_ids: InstallationResolver
    scheduler: ReconcileScheduler
    registrations: RegistrationStore | None = None
    on_ready: Callable[[GitHubHostConfig], Awaitable[None]] | None = None
    state_source: DeploymentStateSource | None = None
    checker: HostChecker | None = None
    api_factory: Callable[[GitHubHostConfig], GitHubRequester] | None = None


@dataclass(slots=True)
class HostRuntime:
    """The GitHub side of one process."""

    monitor: HostMonitor
    signers: Mapping[str, AppKeySigner]
    tokens: InstallationTokenProvider
    gateway: TenantGateway
    deliveries: GitHubHookDeliveries
    registry: HandlerRegistry
    argocd: ArgoCdDeploymentHandler | None


def build_host_runtime(settings: TrustbridgeSettings, deps: RuntimeDeps) -> HostRuntime:
    """Build the signers, monitor, gateway, hook client and handler registry from `settings`."""
    signers = build_signers(settings.signers)
    monitor = HostMonitor(
        settings.github_hosts,
        signers,
        MonitorDeps(
            checker=deps.checker,
            registrations=deps.registrations,
            on_ready=deps.on_ready,
            check_timeout=settings.host_check_timeout_seconds,
        ),
    )
    tokens = InstallationTokenProvider(monitor, signers, deps.api_factory)
    gateway = TenantGateway(
        GatewayDeps(
            monitor=monitor,
            tokens=tokens,
            installations=deps.installation_ids,
            api_factory=deps.api_factory,
        )
    )
    registry = HandlerRegistry()
    register_core_handlers(
        registry,
        InstallationHandlers(deps.installations, deps.scheduler, tokens.invalidate),
    )
    argocd = (
        ArgoCdDeploymentHandler(deps.state_source, DeploymentReporter(gateway))
        if deps.state_source is not None
        else None
    )
    return HostRuntime(
        monitor=monitor,
        signers=signers,
        tokens=tokens,
        gateway=gateway,
        deliveries=GitHubHookDeliveries(monitor, signers, deps.api_factory),
        registry=registry,
        argocd=argocd,
    )
