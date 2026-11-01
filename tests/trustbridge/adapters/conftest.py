"""Fixtures for the adapter core: a fake platform, in-memory stores and a controllable clock."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from trustbridge.adapters.core import AdapterRegistry, CoreDeps, CredentialCore
from trustbridge.adapters.fake import FakePlatform, fake_capabilities, make_fake_adapter
from trustbridge.adapters.interface import (
    AdapterConfig,
    Capabilities,
    GitHubHost,
    HostKind,
    Tenant,
)
from trustbridge.adapters.memory import (
    InMemoryStore,
    InMemoryValueStore,
    MemoryLeases,
    RecordingWriter,
)
from trustbridge.adapters.store import CoreSettings, ScopeId


def pytest_configure(config: pytest.Config) -> None:
    """Register the `integration` marker used by the Postgres-backed tests."""
    config.addinivalue_line("markers", "integration: needs the Postgres service")


PULL = ScopeId(repository_id=10, repository_owner_id=5, environment=None, permission="pull")
PUSH_PROD = ScopeId(repository_id=10, repository_owner_id=5, environment="prod", permission="push")
ADMIN_PROD = ScopeId(
    repository_id=10, repository_owner_id=5, environment="prod", permission="admin"
)
ORG_PUSH = ScopeId(repository_id=None, repository_owner_id=5, environment=None, permission="push")


class Clock:
    """A settable clock."""

    def __init__(self) -> None:
        """Start at a fixed instant."""
        self.now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        """Return the current fake time."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward."""
        self.now += timedelta(seconds=seconds)


class FakeInspector:
    """`EnvironmentInspector` answering from a dict."""

    def __init__(self) -> None:
        """Nothing is protected by default."""
        self.protected: set[tuple[int, str]] = set()

    async def is_protected(self, tenant: Tenant, repository_id: int, environment: str) -> bool:
        """Return whether the environment was marked protected."""
        del tenant
        return (repository_id, environment) in self.protected


class StaticConfigs:
    """`ConfigResolver` returning a config with or without a privileged credential."""

    def __init__(self, *, privileged: bool) -> None:
        """Decide whether tenants have the privileged credential."""
        self.privileged = privileged

    async def config_for(self, tenant: Tenant, adapter: str) -> AdapterConfig:
        """Return the tenant's config."""
        del adapter
        return AdapterConfig(
            tenant, privileged_credential=SecretStr("pat") if self.privileged else None
        )


class FixedTenants:
    """`TenantResolver` over one tenant."""

    def __init__(self, tenant: Tenant) -> None:
        """Hold the tenant."""
        self.tenant = tenant

    async def get_tenant(self, tenant_id: UUID) -> Tenant:
        """Return the tenant."""
        assert tenant_id == self.tenant.id
        return self.tenant


def make_tenant(kind: HostKind = "github.com", *, id_claims: bool = True) -> Tenant:
    """Build a tenant on a host of the given kind."""
    host = GitHubHost(
        name=kind,
        kind=kind,
        api_url="https://api.github.com",
        oidc_issuer="https://token.actions.githubusercontent.com",
        oidc_id_claims=id_claims,
        registry_host="ghcr.io",
    )
    return Tenant(id=uuid4(), github_host=host, org="acme")


@dataclass
class World:
    """Everything a core test touches."""

    core: CredentialCore
    tenant: Tenant
    platform: FakePlatform
    store: InMemoryStore
    values: InMemoryValueStore
    writer: RecordingWriter
    leases: MemoryLeases
    inspector: FakeInspector
    clock: Clock
    deps: CoreDeps
    extra: dict[str, object] = field(default_factory=dict)


def build_world(
    caps: Capabilities | None = None,
    *,
    tenant: Tenant | None = None,
    privileged: bool = True,
    settings: CoreSettings | None = None,
    adapter_class: type | None = None,
) -> World:
    """Build a core wired to in-memory parts and the fake adapter."""
    platform = FakePlatform()
    the_tenant = tenant or make_tenant()
    cls = adapter_class or make_fake_adapter(platform, caps or fake_capabilities(oidc=False))
    store, values, writer = InMemoryStore(), InMemoryValueStore(), RecordingWriter()
    leases, inspector, clock = MemoryLeases(), FakeInspector(), Clock()
    deps = CoreDeps(
        registry=AdapterRegistry({"fake": cls}, StaticConfigs(privileged=privileged)),
        store=store,
        values=values,
        leases=leases,
        writer=writer,
        inspector=inspector,
        tenants=FixedTenants(the_tenant),
        clock=clock,
    )
    return World(
        CredentialCore(deps, settings or CoreSettings()),
        the_tenant,
        platform,
        store,
        values,
        writer,
        leases,
        inspector,
        clock,
        deps,
    )


@pytest.fixture
def secret_world() -> World:
    """A world whose adapter never offers OIDC (secrets, two-phase rotation)."""
    return build_world(fake_capabilities(oidc=False))


@pytest.fixture
def oidc_world() -> World:
    """A world whose adapter supports OIDC on every host kind."""
    return build_world(fake_capabilities(oidc=True))
