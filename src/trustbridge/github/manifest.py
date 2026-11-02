"""GitHub App registration: the manifest flow, and the manual fallback (ADR 0030, "Registration").

The same flow registers two kinds of App:

- `trustbridge`: the deployment's own App for a host. The PEM goes straight to the host's signer
  (`KeyImporter`, or a 0600 file for the `file` kind), the webhook and client secrets to the
  secret store, and the App id plus secret reference to `github_host_registrations`, which every
  process reads at its next recheck, so no restart is needed.
- `argocd`: a separate read-only App (contents: read, metadata) per tenant, owned by the tenant's
  org (ADR 0029 item 6, option B). Its PEM is handed to the tenant's connection data for the
  Argo CD adapter. This path never reads a signer and never touches trustbridge's own key (A14).

Steps (GitHub docs): the browser POSTs the manifest to `/settings/apps/new` (or
`/organizations/ORG/settings/apps/new`) with a `state`; GitHub redirects to `redirect_url` with a
temporary `code` and the same `state`; we exchange it server-side with
`POST /app-manifests/{code}/conversions` within an hour. `state` is a signed, one-time value tied
to the session. The PEM is never logged and never returned to the browser.

GHES 3.18 has no manifest flow for enterprise-owned Apps: `manual_checklist` generates the steps
and `register_manually` takes the App id, PEM and webhook secret through an authenticated admin
endpoint, with the same validations and the same storage.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import quote, urlencode
from uuid import UUID

from pydantic import BaseModel, ConfigDict, SecretStr

from trustbridge.github.client import GitHubApi, GitHubApiError, GitHubRequester
from trustbridge.github.hosts import GitHubHostConfig
from trustbridge.github.models import Registration
from trustbridge.github.ports import NonceStore, SecretWriter, TenantConnectionStore
from trustbridge.signer.base import AppKeySigner, KeyImporter, SignerImportUnsupportedError

STATE_TTL_SECONDS = 3600
"""The conversion code expires after one hour; the signed state gets the same window."""

CALLBACK_PATH = "/admin/github/registration/callback"

TRUSTBRIDGE_PERMISSIONS: dict[str, str] = {
    "metadata": "read",
    "administration": "write",
    "secrets": "write",
    "variables": "write",
    "checks": "write",
    "deployments": "write",
    "organization_secrets": "write",
    "organization_variables": "write",
}
"""`administration: write` is what GitHub requires to create or update an environment."""

TRUSTBRIDGE_EVENTS = ["repository"]
"""`installation` and `installation_repositories` are always delivered to an App."""

ARGOCD_PERMISSIONS: dict[str, str] = {"contents": "read", "metadata": "read"}


class AppKind(StrEnum):
    """Which App a registration flow creates."""

    TRUSTBRIDGE = "trustbridge"
    ARGOCD = "argocd"


class ManifestFlowError(Exception):
    """The flow cannot continue (bad or reused state, no signer import, conversion failed)."""


class RegistrationSink(Protocol):
    """Persists a host's registration (`SqlRegistrationStore`)."""

    async def save(self, host_name: str, registration: Registration) -> None:
        """Insert or replace the registration."""
        ...


class ManifestConversion(BaseModel):
    """The relevant part of `POST /app-manifests/{code}/conversions`. Secrets stay `SecretStr`."""

    model_config = ConfigDict(extra="ignore")

    id: int
    slug: str
    html_url: str
    pem: SecretStr
    webhook_secret: SecretStr | None = None
    client_secret: SecretStr | None = None


@dataclass(frozen=True, slots=True)
class StartRequest:
    """What the admin UI asks for when it starts a flow."""

    kind: AppKind
    session_id: str
    owner: str | None = None
    tenant_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class StartedRegistration:
    """What the UI renders: an auto-submitting form that POSTs `manifest_json` to `post_url`."""

    post_url: str
    manifest_json: str
    state: str


@dataclass(frozen=True, slots=True)
class CompleteRequest:
    """The callback's inputs: the temporary code, the returned state, and the caller's session."""

    code: str
    state: str
    session_id: str


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    """The outcome, safe to return to the browser: ids and references only, no secrets."""

    kind: AppKind
    host: str
    app_id: int
    slug: str
    html_url: str
    webhook_secret_ref: str | None = None
    install_url: str | None = None


@dataclass(frozen=True, slots=True)
class ManualChecklist:
    """The generated steps for registering an App by hand."""

    steps: tuple[str, ...]
    manifest: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ManualApp:
    """The non-key parts of a hand-registered App."""

    app_id: int
    webhook_secret: SecretStr
    slug: str | None = None


@dataclass(frozen=True, slots=True)
class _App:
    app_id: int
    slug: str | None
    html_url: str


@dataclass(frozen=True, slots=True)
class _Secrets:
    webhook: SecretStr
    client: SecretStr | None


@dataclass(frozen=True, slots=True)
class ManifestDeps:
    """Collaborators of `ManifestFlow`."""

    signers: Mapping[str, AppKeySigner]
    secrets: SecretWriter
    registrations: RegistrationSink
    connections: TenantConnectionStore
    nonces: NonceStore
    state_key: bytes
    public_base_url: str
    api_factory: Callable[[GitHubHostConfig], GitHubRequester] | None = None
    clock: Callable[[], float] = time.time


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def webhook_secret_ref(host: str) -> str:
    """The secret-store reference of a host's webhook secret."""
    return f"github-hosts/{host}/webhook-secret"


def client_secret_ref(host: str) -> str:
    """The secret-store reference of a host's OAuth client secret."""
    return f"github-hosts/{host}/client-secret"


class ManifestFlow:
    """Starts and completes App registrations."""

    def __init__(self, deps: ManifestDeps) -> None:
        """Keep the collaborators."""
        self._d = deps

    # ---- manifests ------------------------------------------------------
    def manifest(self, host: GitHubHostConfig, request: StartRequest) -> dict[str, Any]:
        """Build the manifest for `request.kind` from the deployment's base URL."""
        base = self._d.public_base_url.rstrip("/")
        redirect = base + CALLBACK_PATH
        if request.kind is AppKind.TRUSTBRIDGE:
            return {
                "name": f"trustbridge-{host.name}",
                "url": base,
                "hook_attributes": {"url": f"{base}/webhooks/github/{host.name}", "active": True},
                "redirect_url": redirect,
                "public": False,
                "default_events": list(TRUSTBRIDGE_EVENTS),
                "default_permissions": dict(TRUSTBRIDGE_PERMISSIONS),
            }
        owner = request.owner or "org"
        return {
            "name": f"trustbridge-argocd-{owner}",
            "url": base,
            "redirect_url": redirect,
            "public": False,
            "default_permissions": dict(ARGOCD_PERMISSIONS),
        }

    # ---- state ----------------------------------------------------------
    def _sign(self, claims: Mapping[str, Any]) -> str:
        body = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
        mac = hmac.new(self._d.state_key, body.encode(), hashlib.sha256).digest()
        return f"{body}.{_b64(mac)}"

    def _decode(self, state: str) -> dict[str, Any]:
        body, _, mac = state.partition(".")
        expected = hmac.new(self._d.state_key, body.encode(), hashlib.sha256).digest()
        try:
            given = _unb64(mac)
            claims: dict[str, Any] = json.loads(_unb64(body))
        except ValueError as exc:
            msg = "malformed state"
            raise ManifestFlowError(msg) from exc
        if not hmac.compare_digest(expected, given):
            msg = "state signature mismatch"
            raise ManifestFlowError(msg)
        if float(claims.get("exp", 0)) < self._d.clock():
            msg = "state expired"
            raise ManifestFlowError(msg)
        return claims

    # ---- start ----------------------------------------------------------
    async def start(self, host: GitHubHostConfig, request: StartRequest) -> StartedRegistration:
        """Prepare the form the browser submits to GitHub."""
        if request.kind is AppKind.TRUSTBRIDGE:
            self._importer(host)  # fail now: the one-time code cannot be exchanged twice
        elif request.tenant_id is None or request.owner is None:
            msg = "an Argo CD App needs the tenant and its owning org"
            raise ManifestFlowError(msg)
        nonce = secrets.token_urlsafe(24)
        await self._d.nonces.issue(nonce, STATE_TTL_SECONDS)
        state = self._sign(
            {
                "sid": request.session_id,
                "host": host.name,
                "kind": request.kind.value,
                "tenant": str(request.tenant_id) if request.tenant_id else None,
                "nonce": nonce,
                "exp": self._d.clock() + STATE_TTL_SECONDS,
            }
        )
        path = (
            f"/organizations/{quote(request.owner, safe='')}/settings/apps/new"
            if request.owner
            else "/settings/apps/new"
        )
        return StartedRegistration(
            post_url=f"{host.web_url.rstrip('/')}{path}?{urlencode({'state': state})}",
            manifest_json=json.dumps(self.manifest(host, request)),
            state=state,
        )

    def _importer(self, host: GitHubHostConfig) -> KeyImporter:
        signer: object = self._d.signers.get(host.signer)
        if not isinstance(signer, KeyImporter):
            msg = f"signer {host.signer!r} cannot import a key; import the App key out of band"
            raise SignerImportUnsupportedError(msg)
        return signer

    # ---- complete -------------------------------------------------------
    async def complete(
        self, host: GitHubHostConfig, request: CompleteRequest
    ) -> RegistrationResult:
        """Verify the state, exchange the code, and store the result by kind."""
        claims = self._decode(request.state)
        if not hmac.compare_digest(str(claims["sid"]).encode(), request.session_id.encode()):
            msg = "state belongs to another session"
            raise ManifestFlowError(msg)
        if claims["host"] != host.name:
            msg = "state belongs to another host"
            raise ManifestFlowError(msg)
        kind = AppKind(claims["kind"])
        if kind is AppKind.TRUSTBRIDGE:
            self._importer(host)
        if not await self._d.nonces.consume(str(claims["nonce"])):
            msg = "state was already used"
            raise ManifestFlowError(msg)
        conversion = await self._convert(host, request.code)
        if kind is AppKind.TRUSTBRIDGE:
            return await self._store_trustbridge(host, conversion, self._importer(host))
        return await self._store_argocd(host, conversion, UUID(str(claims["tenant"])))

    async def _convert(self, host: GitHubHostConfig, code: str) -> ManifestConversion:
        api = (self._d.api_factory or GitHubApi)(host)
        try:
            resp = await api.request_unauthenticated(
                "POST", f"/app-manifests/{quote(code, safe='')}/conversions"
            )
        except GitHubApiError as exc:
            msg = f"GitHub refused the manifest code (HTTP {exc.status})"
            raise ManifestFlowError(msg) from exc
        return ManifestConversion.model_validate(resp.data)

    async def _store_trustbridge(
        self, host: GitHubHostConfig, conv: ManifestConversion, importer: KeyImporter
    ) -> RegistrationResult:
        # The PEM first: it exists nowhere else. If a later step fails, delete the App on GitHub
        # and register again; the message names the App slug, never any secret.
        await importer.import_key(conv.pem)
        if conv.webhook_secret is None:
            msg = f"GitHub returned no webhook secret for App {conv.slug!r}"
            raise ManifestFlowError(msg)
        return await self._finish(
            host,
            _App(conv.id, conv.slug, conv.html_url),
            _Secrets(conv.webhook_secret, conv.client_secret),
        )

    async def _finish(
        self, host: GitHubHostConfig, app: _App, secrets_in: _Secrets
    ) -> RegistrationResult:
        ref = webhook_secret_ref(host.name)
        await self._d.secrets.write(ref, secrets_in.webhook)
        if secrets_in.client is not None:
            await self._d.secrets.write(client_secret_ref(host.name), secrets_in.client)
        await self._d.registrations.save(host.name, Registration(app.app_id, ref, app.slug))
        return RegistrationResult(
            kind=AppKind.TRUSTBRIDGE,
            host=host.name,
            app_id=app.app_id,
            slug=app.slug or "",
            html_url=app.html_url,
            webhook_secret_ref=ref,
        )

    async def _store_argocd(
        self, host: GitHubHostConfig, conv: ManifestConversion, tenant_id: UUID
    ) -> RegistrationResult:
        await self._d.connections.put(
            tenant_id,
            "argocd",
            {"app_id": SecretStr(str(conv.id)), "private_key": conv.pem},
        )
        return RegistrationResult(
            kind=AppKind.ARGOCD,
            host=host.name,
            app_id=conv.id,
            slug=conv.slug,
            html_url=conv.html_url,
            install_url=f"{conv.html_url.rstrip('/')}/installations/new",
        )

    async def record_argocd_installation(self, tenant_id: UUID, installation_id: int) -> None:
        """Store the installation id of the tenant's Argo CD App once the org installed it."""
        await self._d.connections.put(
            tenant_id, "argocd", {"installation_id": SecretStr(str(installation_id))}
        )

    # ---- manual fallback ------------------------------------------------
    def manual_checklist(self, host: GitHubHostConfig) -> ManualChecklist:
        """Steps for a host where the flow is unavailable (GHES 3.18 enterprise-owned Apps)."""
        manifest = self.manifest(host, StartRequest(AppKind.TRUSTBRIDGE, session_id=""))
        perms = ", ".join(f"{k}: {v}" for k, v in manifest["default_permissions"].items())
        steps = (
            f"Open {host.web_url.rstrip('/')} as an enterprise or organization owner and create a "
            "new GitHub App (Settings > Developer settings > GitHub Apps > New GitHub App).",
            f"Webhook URL: {manifest['hook_attributes']['url']} (active, SSL verification on).",
            "Webhook secret: generate a long random value and keep it for step 6.",
            f"Subscribe to events: {', '.join(manifest['default_events'])}.",
            f"Repository and organization permissions: {perms}.",
            "Generate a private key, then submit the App id, the PEM and the webhook secret to the "
            "authenticated admin endpoint (or the CLI). The PEM is imported straight into the "
            f"signer {host.signer!r} and is not stored anywhere else.",
        )
        return ManualChecklist(steps, manifest)

    async def register_manually(
        self, host: GitHubHostConfig, app: ManualApp, pem: SecretStr
    ) -> RegistrationResult:
        """Store a hand-registered App exactly as the manifest flow would."""
        importer = self._importer(host)
        await importer.import_key(pem)
        return await self._finish(
            host,
            _App(app.app_id, app.slug, host.web_url.rstrip("/")),
            _Secrets(app.webhook_secret, None),
        )
