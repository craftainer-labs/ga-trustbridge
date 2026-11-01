"""A fake platform adapter that follows the whole I2 contract, for NG5 and the core tests.

`FakePlatform` is the "remote platform": a dict of named objects plus fault injection.
`make_fake_adapter` returns an adapter class bound to a platform (the loader instantiates
classes with `cls(config)`). It is not registered under the `trustbridge.adapters` entry-point
group, so it is never loaded by a deployment.
"""

import asyncio
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import ClassVar, Literal

from pydantic import SecretStr

from trustbridge.adapters.core import expected_claims
from trustbridge.adapters.interface import (
    API_VERSION,
    AdapterConfig,
    AdapterError,
    Applied,
    Capabilities,
    Change,
    ChangeState,
    CredentialOutcome,
    CredentialScope,
    CredentialSlot,
    CredentialState,
    GitHubOutput,
    Health,
    HostKind,
    IssuedSecret,
    ListedCredential,
    NoCiCredential,
    OidcTrust,
    ProjectRef,
    ProjectSpec,
    RepoSpec,
    ResourceState,
    Tenant,
    WebhookTarget,
    tenant_prefix,
)

ALL_KINDS: frozenset[HostKind] = frozenset({"github.com", "ghes", "ghe.com"})


def fake_capabilities(
    *,
    oidc: bool = True,
    rotation: Literal["none", "single", "two_phase"] = "two_phase",
    ci_credential: bool = True,
    needs_privileged_credential: bool = False,
    credential_limit: int | None = None,
) -> Capabilities:
    """Return capabilities for a fake adapter."""
    return Capabilities(
        oidc=oidc,
        oidc_hosts=ALL_KINDS if oidc else frozenset(),
        oidc_requires_manual_step=False,
        ci_credential=ci_credential,
        rotation=rotation,
        webhook=None,
        github_hosts=ALL_KINDS,
        needs_privileged_credential=needs_privileged_credential,
        credential_limit=credential_limit,
    )


@dataclass(slots=True)
class FakePlatform:
    """The fake remote platform: named credentials and resources, calls, and injected faults."""

    credentials: dict[str, dict[str, str]] = field(default_factory=dict)
    resources: dict[str, dict[str, str]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    fail_before: dict[str, list[AdapterError]] = field(default_factory=dict)
    fail_after: dict[str, list[AdapterError]] = field(default_factory=dict)
    manual_action: str | None = None
    hang: set[str] = field(default_factory=set)
    on_enter: Callable[[str], Awaitable[None]] | None = None
    _counter: int = 0

    def new_id(self) -> str:
        """Return a fresh platform id."""
        self._counter += 1
        return f"id-{self._counter}"

    async def enter(self, method: str) -> None:
        """Record a call and apply the faults queued before its side effect."""
        self.calls.append(method)
        if self.on_enter is not None:
            await self.on_enter(method)
        if method in self.hang:
            await asyncio.sleep(3600)
        queue = self.fail_before.get(method)
        if queue:
            raise queue.pop(0)

    def leave(self, method: str) -> None:
        """Apply the faults queued after the side effect (a lost response)."""
        queue = self.fail_after.get(method)
        if queue:
            raise queue.pop(0)


def make_fake_adapter(
    platform: FakePlatform,
    caps: Capabilities | None = None,
    adapter_name: str = "fake",
) -> type[FakeAdapter]:
    """Return a `FakeAdapter` subclass bound to `platform` and `caps` (a loader-ready class)."""
    capabilities = caps or fake_capabilities()

    class _Bound(FakeAdapter):
        name: ClassVar[str] = adapter_name
        _platform = platform
        _caps = capabilities

    return _Bound


class FakeAdapter:
    """An in-memory adapter honouring invariants A1-A6 and A10."""

    name: ClassVar[str] = "fake"
    api_version: ClassVar[int] = API_VERSION
    _platform: ClassVar[FakePlatform]
    _caps: ClassVar[Capabilities]

    def __init__(self, config: AdapterConfig) -> None:
        """Keep the config (the fake uses none of it)."""
        self._config = config

    def capabilities(self) -> Capabilities:
        """Return the bound capabilities."""
        return self._caps

    # ---- resources -------------------------------------------------------
    async def _ensure_resource(
        self, method: str, prior: ResourceState | None, name: str
    ) -> Applied:
        p = self._platform
        await p.enter(method)
        wanted = prior.name if prior is not None else name
        existing = p.resources.get(wanted)
        if existing is None:
            existing = {"id": p.new_id()}
            p.resources[wanted] = existing
            change = ChangeState.CREATED
        else:
            change = ChangeState.UNCHANGED
        p.leave(method)
        return Applied(Change(change), ResourceState(name=wanted, ref=dict(existing)))

    async def ensure_project(
        self, t: Tenant, s: ProjectSpec, prior: ResourceState | None
    ) -> Applied:
        """Create the project if absent."""
        return await self._ensure_resource("ensure_project", prior, f"{tenant_prefix(t.id)}{s.key}")

    async def ensure_repository(
        self, p: ProjectRef, s: RepoSpec, prior: ResourceState | None
    ) -> Applied:
        """Create the repository if absent."""
        return await self._ensure_resource("ensure_repository", prior, f"{p.project.name}/{s.key}")

    async def ensure_webhook(self, t: WebhookTarget, prior: ResourceState | None) -> Applied:
        """Create the webhook if absent."""
        return await self._ensure_resource(
            "ensure_webhook", prior, f"{tenant_prefix(t.tenant.id)}hook"
        )

    async def delete(self, prior: ResourceState) -> Applied:
        """Delete a resource; absent is UNCHANGED."""
        await self._platform.enter("delete")
        gone = self._platform.resources.pop(prior.name, None)
        return Applied(Change(ChangeState.DELETED if gone else ChangeState.UNCHANGED), None)

    # ---- credentials -----------------------------------------------------
    def _slot_with_ref(self, slot: CredentialSlot) -> CredentialSlot:
        entry = self._platform.credentials[slot.name]
        return replace(slot, ref={"id": entry["id"]})

    def _create(self, slot: CredentialSlot, kind: str) -> tuple[ChangeState, IssuedSecret | None]:
        """Create or replace the platform object named `slot.name` (the lookup rule)."""
        creds = self._platform.credentials
        existing = creds.get(slot.name)
        value = secrets.token_hex(8) if kind == "secret" else ""
        if existing is None:
            creds[slot.name] = {"id": self._platform.new_id(), "kind": kind, "secret": value}
            change = ChangeState.CREATED
        elif kind == "trust" and existing["kind"] == "trust":
            return ChangeState.UNCHANGED, None
        else:
            existing.update({"kind": kind, "secret": value})
            change = ChangeState.UPDATED
        issued = IssuedSecret({"token": SecretStr(value)}) if kind == "secret" else None
        return change, issued

    async def ensure_credential(
        self, s: CredentialScope, prior: CredentialState
    ) -> CredentialOutcome:
        """Issue a secret, or establish OIDC trust when the core offered it."""
        p = self._platform
        await p.enter("ensure_credential")
        slot = prior.current
        if slot is None:
            raise AdapterError(False, "no current slot")
        if p.manual_action is not None:
            return CredentialOutcome(
                Change(ChangeState.NEEDS_MANUAL_ACTION, manual_action=p.manual_action),
                prior,
                None,
            )
        if not self._caps.ci_credential:
            return CredentialOutcome(
                Change(ChangeState.UNCHANGED),
                CredentialState(current=replace(slot, delivered=True)),
                NoCiCredential("fake: no CI credential"),
            )
        if s.oidc_eligible:
            change, _ = self._create(slot, "trust")
            p.leave("ensure_credential")
            state = CredentialState(
                current=replace(self._slot_with_ref(slot), delivered=True), previous=prior.previous
            )
            trust = OidcTrust(
                issuer=s.github_host.oidc_issuer, audience=None, claims=expected_claims(s)
            )
            return CredentialOutcome(Change(change), state, trust)
        existing = p.credentials.get(slot.name)
        if slot.delivered and existing is not None and existing["kind"] == "secret":
            return CredentialOutcome(Change(ChangeState.UNCHANGED), prior, None)
        change, issued = self._create(slot, "secret")
        p.leave("ensure_credential")
        state = CredentialState(
            current=replace(self._slot_with_ref(slot), delivered=False), previous=prior.previous
        )
        return CredentialOutcome(Change(change), state, issued)

    async def rotate_begin(self, s: CredentialScope, prior: CredentialState) -> CredentialOutcome:
        """Create the next generation (two-phase) or replace in place (single)."""
        p = self._platform
        await p.enter("rotate_begin")
        slot = prior.current
        if slot is None:
            raise AdapterError(False, "no current slot")
        change, issued = self._create(slot, "secret")
        p.leave("rotate_begin")
        previous = prior.previous if self._caps.rotation == "two_phase" else None
        state = CredentialState(
            current=replace(self._slot_with_ref(slot), delivered=False), previous=previous
        )
        del s
        return CredentialOutcome(Change(change), state, issued)

    async def rotate_finish(self, s: CredentialScope, prior: CredentialState) -> CredentialOutcome:
        """Delete `previous` only; idempotent."""
        p = self._platform
        await p.enter("rotate_finish")
        gone = None
        if prior.previous is not None:
            gone = p.credentials.pop(prior.previous.name, None)
        p.leave("rotate_finish")
        del s
        change = ChangeState.DELETED if gone else ChangeState.UNCHANGED
        return CredentialOutcome(Change(change), CredentialState(current=prior.current), None)

    async def delete_credential(
        self, s: CredentialScope, prior: CredentialState
    ) -> CredentialOutcome:
        """Delete the current and previous platform objects; absent is UNCHANGED."""
        p = self._platform
        await p.enter("delete_credential")
        removed = False
        for slot in (prior.current, prior.previous):
            if slot is not None and p.credentials.pop(slot.name, None) is not None:
                removed = True
        p.leave("delete_credential")
        del s
        change = ChangeState.DELETED if removed else ChangeState.UNCHANGED
        return CredentialOutcome(Change(change), CredentialState(current=None), None)

    async def list_credentials(self, t: Tenant) -> Sequence[ListedCredential]:
        """List this tenant's trustbridge-named credentials."""
        await self._platform.enter("list_credentials")
        prefix = tenant_prefix(t.id)
        return [
            ListedCredential(name=n, ref={"id": c["id"]})
            for n, c in self._platform.credentials.items()
            if n.startswith(prefix)
        ]

    def github_outputs(
        self, s: CredentialScope, credential: OidcTrust | IssuedSecret | NoCiCredential
    ) -> Sequence[GitHubOutput]:
        """Return a pure function of `(scope, credential)` (invariant A1)."""
        if isinstance(credential, NoCiCredential):
            return []
        outputs: list[GitHubOutput] = [
            GitHubOutput(
                "variable", "FAKE_REGISTRY", "fake.example", s.repository_id, s.environment
            )
        ]
        if isinstance(credential, IssuedSecret):
            token: Mapping[str, SecretStr] = credential.values
            outputs.append(
                GitHubOutput("secret", "FAKE_TOKEN", token["token"], s.repository_id, s.environment)
            )
        return outputs

    async def health_check(self) -> Health:
        """Always healthy."""
        return Health(ok=True)
