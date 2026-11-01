"""The manifest flow (and manual fallback), and the installation lifecycle handlers."""

import json
import logging
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from tests.trustbridge.github.conftest import (
    FakeChecker,
    KeySigner,
    ScriptedGitHub,
    make_host_config,
)
from tests.trustbridge.signer.test_signers import make_key, pem_of
from trustbridge.github.availability import HostMonitor, HostState, MonitorDeps
from trustbridge.github.client import GitHubApi, GitHubRequester
from trustbridge.github.handlers import (
    EventContext,
    HandlerRegistry,
    InstallationHandlers,
    register_core_handlers,
)
from trustbridge.github.hosts import GitHubHostConfig
from trustbridge.github.manifest import (
    ARGOCD_PERMISSIONS,
    CALLBACK_PATH,
    AppKind,
    CompleteRequest,
    ManifestDeps,
    ManifestFlow,
    ManifestFlowError,
    ManualApp,
    StartRequest,
)
from trustbridge.github.models import Registration
from trustbridge.signer.base import AppKeySigner, SignerImportUnsupportedError
from trustbridge.signer.file import FileSigner

PEM = pem_of(make_key())
WEBHOOK_REF = "github-hosts/github.com/webhook-secret"


class Nonces:
    """One-time values."""

    def __init__(self) -> None:
        """Start empty."""
        self.live: set[str] = set()

    async def issue(self, nonce: str, ttl_seconds: int) -> None:
        """Remember a nonce."""
        del ttl_seconds
        self.live.add(nonce)

    async def consume(self, nonce: str) -> bool:
        """True exactly once."""
        if nonce in self.live:
            self.live.discard(nonce)
            return True
        return False


class Secrets:
    """Records secret-store writes."""

    def __init__(self) -> None:
        """Start empty."""
        self.items: dict[str, str] = {}

    async def write(self, ref: str, value: SecretStr) -> None:
        """Store a secret."""
        self.items[ref] = value.get_secret_value()


class Regs:
    """Records registrations."""

    def __init__(self) -> None:
        """Start empty."""
        self.items: dict[str, Registration] = {}

    async def save(self, host_name: str, registration: Registration) -> None:
        """Store a registration."""
        self.items[host_name] = registration


class Connections:
    """Records tenant connection data."""

    def __init__(self) -> None:
        """Start empty."""
        self.items: dict[tuple[UUID, str], dict[str, str]] = {}

    async def put(self, tenant_id: UUID, adapter: str, values: Mapping[str, SecretStr]) -> None:
        """Merge values."""
        self.items.setdefault((tenant_id, adapter), {}).update(
            {k: v.get_secret_value() for k, v in values.items()}
        )


class ExplodingSigners(Mapping[str, AppKeySigner]):
    """Any lookup fails: the Argo CD path must never touch a signer (A14)."""

    def __getitem__(self, key: str) -> AppKeySigner:
        """Fail."""
        raise AssertionError("the Argo CD flow must not read a signer")

    def __iter__(self) -> Iterator[str]:
        """No names."""
        return iter(())

    def __len__(self) -> int:
        """Empty."""
        return 0


class Flow:
    """A manifest flow with recording collaborators and a scripted GitHub."""

    def __init__(
        self, tmp_path: Path, *, signers: Mapping[str, AppKeySigner] | None = None
    ) -> None:
        """Wire the flow."""
        self.gh = ScriptedGitHub()
        self.signer_path = tmp_path / "app.pem"
        self.file_signer = FileSigner(self.signer_path)
        self.secrets, self.regs = Secrets(), Regs()
        self.conns, self.nonces = Connections(), Nonces()
        self.now = 1_800_000_000.0

        def factory(host: GitHubHostConfig) -> GitHubRequester:
            return GitHubApi(host, async_transport=self.gh.transport())

        self.flow = ManifestFlow(
            ManifestDeps(
                signers=signers if signers is not None else {"main": self.file_signer},
                secrets=self.secrets,
                registrations=self.regs,
                connections=self.conns,
                nonces=self.nonces,
                state_key=b"k" * 32,
                public_base_url="https://tb.example",
                api_factory=factory,
                clock=lambda: self.now,
            )
        )
        self.host = make_host_config(app_id=None)

    def script_conversion(self, code: str = "c0de") -> None:
        """GitHub answers the code exchange."""
        self.gh.route(
            "POST",
            f"/app-manifests/{code}/conversions",
            201,
            {
                "id": 123,
                "slug": "trustbridge-github-com",
                "html_url": "https://github.com/apps/trustbridge-github-com",
                "pem": PEM,
                "webhook_secret": "whsec",
                "client_secret": "csec",
                "client_id": "Iv1.x",
            },
        )


def state_of(started_url: str) -> str:
    """The `state` query parameter of the post URL."""
    return parse_qs(urlparse(started_url).query)["state"][0]


async def test_trustbridge_flow_stores_key_secrets_and_registration(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Start, callback, exchange: the PEM goes to the signer, the rest to the stores; no leak."""
    caplog.set_level(logging.DEBUG)
    f = Flow(tmp_path)
    f.script_conversion()
    started = await f.flow.start(f.host, StartRequest(AppKind.TRUSTBRIDGE, "sess"))
    assert started.post_url.startswith("https://github.com/settings/apps/new?state=")
    manifest = json.loads(started.manifest_json)
    assert manifest["hook_attributes"]["url"] == "https://tb.example/webhooks/github/github.com"
    assert manifest["redirect_url"] == "https://tb.example" + CALLBACK_PATH
    assert manifest["default_permissions"]["secrets"] == "write"
    result = await f.flow.complete(
        f.host, CompleteRequest("c0de", state_of(started.post_url), "sess")
    )
    assert result.app_id == 123
    assert result.webhook_secret_ref == WEBHOOK_REF
    assert f.regs.items["github.com"] == Registration(
        123, result.webhook_secret_ref or "", "trustbridge-github-com"
    )
    assert f.secrets.items["github-hosts/github.com/webhook-secret"] == "whsec"
    assert f.secrets.items["github-hosts/github.com/client-secret"] == "csec"
    assert f.signer_path.read_text() == PEM
    assert oct(f.signer_path.stat().st_mode & 0o777) == "0o600"
    # Invariant 5: no PEM or secret in the result (what the browser gets) or in any log record.
    assert PEM not in repr(result)
    assert "whsec" not in repr(result)
    assert all(PEM not in r.getMessage() and "whsec" not in r.getMessage() for r in caplog.records)
    # The recorded GitHub exchange was unauthenticated.
    assert "Authorization" not in f.gh.requests[0].headers


async def test_org_owned_apps_post_to_the_organization_route(tmp_path: Path) -> None:
    """An org owner registers via /organizations/ORG/settings/apps/new."""
    f = Flow(tmp_path)
    started = await f.flow.start(f.host, StartRequest(AppKind.TRUSTBRIDGE, "s", owner="acme"))
    assert urlparse(started.post_url).path == "/organizations/acme/settings/apps/new"


@pytest.mark.parametrize("problem", ["session", "host", "reuse", "expired", "tamper"])
async def test_state_is_session_bound_signed_one_time_and_expires(
    tmp_path: Path, problem: str
) -> None:
    """CSRF protection: every way the state can be wrong is refused before the code is used."""
    f = Flow(tmp_path)
    f.script_conversion()
    started = await f.flow.start(f.host, StartRequest(AppKind.TRUSTBRIDGE, "sess"))
    state = state_of(started.post_url)
    host, session = f.host, "sess"
    if problem == "session":
        session = "other"
    elif problem == "host":
        host = make_host_config("elsewhere", app_id=None)
    elif problem == "expired":
        f.now += 3601
    elif problem == "tamper":
        state = state[:-3] + ("AAA" if not state.endswith("AAA") else "BBB")
    elif problem == "reuse":
        await f.flow.complete(f.host, CompleteRequest("c0de", state, "sess"))
    with pytest.raises(ManifestFlowError):
        await f.flow.complete(host, CompleteRequest("c0de", state, session))


async def test_github_refusing_the_code_is_a_flow_error(tmp_path: Path) -> None:
    """An expired or reused code (404/422 from GitHub) is reported, not leaked."""
    f = Flow(tmp_path)
    f.gh.route("POST", "/app-manifests/c0de/conversions", 404, {"message": "Not Found"})
    started = await f.flow.start(f.host, StartRequest(AppKind.TRUSTBRIDGE, "s"))
    with pytest.raises(ManifestFlowError, match="HTTP 404"):
        await f.flow.complete(f.host, CompleteRequest("c0de", state_of(started.post_url), "s"))


async def test_a_signer_that_cannot_import_is_refused_before_the_code_is_spent(
    tmp_path: Path,
) -> None:
    """The one-time code is never exchanged when the key has nowhere to go."""
    f = Flow(tmp_path, signers={"main": KeySigner()})
    with pytest.raises(SignerImportUnsupportedError):
        await f.flow.start(f.host, StartRequest(AppKind.TRUSTBRIDGE, "s"))
    assert f.gh.requests == []


async def test_argocd_app_is_registered_per_tenant_without_touching_any_signer(
    tmp_path: Path,
) -> None:
    """A14: the read-only App's key goes to the tenant's connection data; no signer is read."""
    f = Flow(tmp_path, signers=ExplodingSigners())
    f.script_conversion()
    tenant = uuid4()
    request = StartRequest(AppKind.ARGOCD, "s", owner="acme", tenant_id=tenant)
    started = await f.flow.start(f.host, request)
    assert urlparse(started.post_url).path == "/organizations/acme/settings/apps/new"
    manifest = json.loads(started.manifest_json)
    assert manifest["default_permissions"] == ARGOCD_PERMISSIONS
    assert "hook_attributes" not in manifest
    result = await f.flow.complete(f.host, CompleteRequest("c0de", state_of(started.post_url), "s"))
    assert result.kind is AppKind.ARGOCD
    assert result.install_url == "https://github.com/apps/trustbridge-github-com/installations/new"
    assert f.conns.items[(tenant, "argocd")] == {"app_id": "123", "private_key": PEM}
    assert f.regs.items == {}
    assert PEM not in repr(result)
    await f.flow.record_argocd_installation(tenant, 99)
    assert f.conns.items[(tenant, "argocd")]["installation_id"] == "99"


async def test_argocd_flow_needs_a_tenant_and_owner(tmp_path: Path) -> None:
    """The Argo CD App is per tenant and owned by the tenant's org."""
    f = Flow(tmp_path)
    with pytest.raises(ManifestFlowError):
        await f.flow.start(f.host, StartRequest(AppKind.ARGOCD, "s"))


async def test_manual_registration_matches_the_manifest_flow_and_the_host_becomes_ready(
    tmp_path: Path,
) -> None:
    """GHES 3.18 fallback: checklist, then the same storage; a monitor picks it up."""
    f = Flow(tmp_path)
    checklist = f.flow.manual_checklist(f.host)
    assert any("https://tb.example/webhooks/github/github.com" in s for s in checklist.steps)
    assert any("administration: write" in s for s in checklist.steps)
    result = await f.flow.register_manually(
        f.host, ManualApp(app_id=321, webhook_secret=SecretStr("whsec"), slug="s"), SecretStr(PEM)
    )
    assert result.app_id == 321
    assert f.signer_path.read_text() == PEM

    class Store:
        async def load_all(self) -> Mapping[str, Registration]:
            return f.regs.items

    monitor = HostMonitor(
        [f.host],
        {"main": f.file_signer},
        MonitorDeps(checker=FakeChecker(app_id=321), registrations=Store()),
    )
    await monitor.check_all()
    assert monitor.status("github.com").state is HostState.READY


# ---- handlers -------------------------------------------------------------------------------
class Store:
    """Records installation bookkeeping."""

    def __init__(self) -> None:
        """Start empty."""
        self.tenants: dict[tuple[str, int], UUID] = {}
        self.states: dict[UUID, str] = {}
        self.repos: dict[UUID, set[int]] = {}
        self.names: dict[tuple[UUID, int], str] = {}

    async def upsert_installation(
        self, host: str, installation_id: int, org: str, org_id: int
    ) -> UUID:
        """Create the tenant."""
        del org, org_id
        tenant = self.tenants.setdefault((host, installation_id), uuid4())
        self.states[tenant] = "active"
        return tenant

    async def tenant_id_for(self, host: str, installation_id: int) -> UUID | None:
        """Look the tenant up."""
        return self.tenants.get((host, installation_id))

    async def set_installation_state(
        self, host: str, installation_id: int, state: str
    ) -> UUID | None:
        """Set the state."""
        tenant = self.tenants.get((host, installation_id))
        if tenant is not None:
            self.states[tenant] = state
        return tenant

    async def apply_repository_changes(
        self, tenant_id: UUID, added: Sequence[tuple[int, str]], removed: Sequence[int]
    ) -> None:
        """Apply added and removed repositories."""
        repos = self.repos.setdefault(tenant_id, set())
        repos.update(i for i, _ in added)
        repos.difference_update(removed)

    async def rename_repository(self, tenant_id: UUID, repository_id: int, full_name: str) -> None:
        """Record a rename."""
        self.names[(tenant_id, repository_id)] = full_name


class Scheduler:
    """Records scheduled reconciles."""

    def __init__(self) -> None:
        """Start empty."""
        self.scheduled: list[UUID] = []

    async def schedule(self, tenant_id: UUID) -> None:
        """Record."""
        self.scheduled.append(tenant_id)


def ctx(event: str, action: str | None, payload: dict[str, object]) -> EventContext:
    """An event context on host github.com."""
    return EventContext("github.com", "d1", event, action, payload)


INSTALLATION = {"id": 4242, "account": {"login": "acme", "id": 5}}


async def test_installation_lifecycle_events_drive_tenants_and_reconciles() -> None:
    """created / suspend / unsuspend / new_permissions_accepted / deleted."""
    store, sched = Store(), Scheduler()
    dropped: list[tuple[str, int]] = []
    registry = HandlerRegistry()
    register_core_handlers(
        registry, InstallationHandlers(store, sched, lambda h, i: dropped.append((h, i)))
    )
    created = ctx(
        "installation",
        "created",
        {"installation": INSTALLATION, "repositories": [{"id": 10, "full_name": "acme/web"}]},
    )
    assert await registry.dispatch(created) == 1
    tenant = store.tenants[("github.com", 4242)]
    assert store.repos[tenant] == {10}
    assert sched.scheduled == [tenant]
    for action, state in [
        ("suspend", "suspended"),
        ("unsuspend", "active"),
        ("deleted", "deleted"),
    ]:
        await registry.dispatch(ctx("installation", action, {"installation": INSTALLATION}))
        assert store.states[tenant] == state
    assert sched.scheduled == [tenant, tenant]  # created and unsuspend
    await registry.dispatch(
        ctx("installation", "new_permissions_accepted", {"installation": INSTALLATION})
    )
    assert sched.scheduled[-1] == tenant
    assert len(dropped) == 4


async def test_repository_selection_and_repository_events() -> None:
    """installation_repositories add/remove; repository renames and deletions use the numeric id."""
    store, sched = Store(), Scheduler()
    handlers = InstallationHandlers(store, sched)
    await handlers.on_installation(ctx("installation", "created", {"installation": INSTALLATION}))
    tenant = store.tenants[("github.com", 4242)]
    await handlers.on_installation_repositories(
        ctx(
            "installation_repositories",
            "added",
            {
                "installation": INSTALLATION,
                "repositories_added": [
                    {"id": 1, "full_name": "acme/a"},
                    {"id": 2, "full_name": "acme/b"},
                ],
                "repositories_removed": [],
            },
        )
    )
    await handlers.on_installation_repositories(
        ctx(
            "installation_repositories",
            "removed",
            {
                "installation": INSTALLATION,
                "repositories_added": [],
                "repositories_removed": [{"id": 2}],
            },
        )
    )
    assert store.repos[tenant] == {1}
    await handlers.on_repository(
        ctx(
            "repository",
            "renamed",
            {"installation": INSTALLATION, "repository": {"id": 1, "full_name": "acme/z"}},
        )
    )
    assert store.names[(tenant, 1)] == "acme/z"
    await handlers.on_repository(
        ctx(
            "repository",
            "deleted",
            {"installation": INSTALLATION, "repository": {"id": 1, "full_name": "acme/z"}},
        )
    )
    assert store.repos[tenant] == set()
    before = len(sched.scheduled)
    await handlers.on_repository(
        ctx(
            "repository",
            "created",
            {"installation": INSTALLATION, "repository": {"id": 3, "full_name": "acme/q"}},
        )
    )
    assert len(sched.scheduled) == before


async def test_unknown_events_and_installations_are_ignored() -> None:
    """The registry returns 0 for an event nobody handles; unknown installations are skipped."""
    registry = HandlerRegistry()
    assert await registry.dispatch(ctx("star", None, {})) == 0
    handlers = InstallationHandlers(Store(), Scheduler())
    await handlers.on_installation_repositories(
        ctx("installation_repositories", "added", {"installation": INSTALLATION})
    )
    await handlers.on_repository(ctx("repository", "renamed", {"installation": None}))
    await handlers.on_installation(ctx("installation", "weird", {"installation": INSTALLATION}))
