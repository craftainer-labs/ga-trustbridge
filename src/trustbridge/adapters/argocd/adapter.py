"""The Argo CD adapter (NG5d, ADR 0029).

Argo CD is driven through the Kubernetes API of its cluster (ADR 0029, capability matrix): the
adapter reads and writes Secrets, ConfigMaps and AppProjects in the Argo CD namespace with a
ServiceAccount token. See `README.md` for the objects, the sources and what is unconfirmed.
"""

import base64
import json
import re
from collections.abc import Sequence
from dataclasses import replace
from typing import ClassVar
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from trustbridge.adapters.argocd.client import JsonObj, KubeClient, json_object, seg
from trustbridge.adapters.interface import (
    API_VERSION,
    AdapterConfig,
    AdapterConfigurationError,
    AdapterError,
    Applied,
    Capabilities,
    Change,
    ChangeState,
    CredentialOutcome,
    CredentialScope,
    CredentialSlot,
    CredentialState,
    GitHubHost,
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
    WebhookAuth,
    WebhookCapability,
    WebhookTarget,
    resource_name,
    tenant_prefix,
)

HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
NOT_FOUND = frozenset({HTTP_NOT_FOUND})
CONFLICT = frozenset({HTTP_CONFLICT})

DEFAULT_NAMESPACE = "argocd"
SECRET_TYPE_LABEL = "argocd.argoproj.io/secret-type"  # noqa: S105 - a label key, not a secret
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY = "trustbridge"
NOTIFICATIONS_CM = "argocd-notifications-cm"
NOTIFICATIONS_SECRET = "argocd-notifications-secret"  # noqa: S105 - an object name
WEBHOOK_HEADER = "Authorization"
NO_CI_REASON = "Argo CD pulls from Git with the tenant's read-only App; CI needs no credential"
INSTALL_APP_ACTION = (
    "Install the tenant's read-only Argo CD GitHub App on the organization; "
    "no installation id is recorded yet"
)

_GITHUB_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")


def web_base(host: GitHubHost) -> str:
    """Return the web URL of a GitHub host, derived from its API URL.

    `https://api.github.com` gives `https://github.com`, `https://api.SUB.ghe.com` gives
    `https://SUB.ghe.com`, and a GHES `https://host/api/v3` gives `https://host`.
    """
    url = host.api_url.rstrip("/").removesuffix("/api/v3")
    return url if host.kind == "ghes" else url.replace("://api.", "://", 1)


def _name_part(value: str, what: str) -> str:
    if not _GITHUB_NAME.fullmatch(value) or value in {".", ".."}:
        raise AdapterConfigurationError(f"{what} is not a valid GitHub name")
    return value


def _marked(obj: JsonObj) -> bool:
    """Return whether the adapter created the object (it carries the managed-by label)."""
    labels = (obj.get("metadata") or {}).get("labels") or {}
    return bool(labels.get(MANAGED_BY_LABEL) == MANAGED_BY)


def _collision(what: str) -> AdapterError:
    """Refuse an object of the generated name that the adapter did not create."""
    return AdapterError(False, f"{what} exists but is not managed by trustbridge; not adopting it")


def _decoded(obj: JsonObj, key: str) -> str | None:
    raw = (obj.get("data") or {}).get(key)
    return base64.b64decode(raw).decode() if isinstance(raw, str) else None


def _webhook_service(url: str, token_key: str) -> str:
    """Return the `service.webhook.<name>` value: the URL and a bearer header (a key reference)."""
    header = f"- name: {WEBHOOK_HEADER}\n  value: Bearer ${token_key}\n"
    return f"url: {json.dumps(url)}\nheaders:\n{header}"


class ArgoCDAdapter:
    """Provisions Argo CD AppProjects, repository credentials and the notifications webhook."""

    name: ClassVar[str] = "argocd"
    api_version: ClassVar[int] = API_VERSION

    def __init__(
        self, config: AdapterConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        """Keep the per-tenant config; `transport` exists for the contract tests."""
        self._config = config
        self._transport = transport

    # ---- configuration ---------------------------------------------------
    def capabilities(self) -> Capabilities:
        """Describe Argo CD: no OIDC, no CI credential, no rotation, a static-header webhook."""
        kinds: frozenset[HostKind] = frozenset({"github.com", "ghes", "ghe.com"})
        return Capabilities(
            oidc=False,
            oidc_hosts=frozenset(),
            oidc_requires_manual_step=False,
            ci_credential=False,
            rotation="none",
            webhook=WebhookCapability(WebhookAuth.STATIC_HEADER, WEBHOOK_HEADER, trigger_only=True),
            github_hosts=kinds,
            needs_privileged_credential=True,
        )

    def _client(self) -> KubeClient:
        token = self._config.privileged_credential
        if token is None:
            raise AdapterConfigurationError("the Argo CD ServiceAccount token is not configured")
        raw = self._config.connection.get("base_url")
        if raw is None:
            raise AdapterConfigurationError("Argo CD connection data has no base_url")
        url = raw.get_secret_value().rstrip("/")
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.netloc or parts.query or parts.fragment:
            raise AdapterConfigurationError("Argo CD base_url must be an https URL without a query")
        return KubeClient(url, token, self._transport)

    def _namespace(self) -> str:
        override = self._config.connection.get("namespace")
        return override.get_secret_value() if override is not None else DEFAULT_NAMESPACE

    def _core(self, kind: str) -> str:
        return f"/api/v1/namespaces/{seg(self._namespace())}/{kind}"

    def _projects(self) -> str:
        return f"/apis/argoproj.io/v1alpha1/namespaces/{seg(self._namespace())}/appprojects"

    @staticmethod
    def _repo_url(tenant: Tenant, key: str | None) -> str:
        """Return the Git URL of one repository, or the prefix of the whole org (trailing `/`)."""
        base = f"{web_base(tenant.github_host)}/{_name_part(tenant.org, 'the organization')}/"
        return base if key is None else base + _name_part(key, "repository key")

    # ---- projects --------------------------------------------------------
    async def ensure_project(
        self, t: Tenant, s: ProjectSpec, prior: ResourceState | None
    ) -> Applied:
        """Create the AppProject (no source repos, no destinations), or fix its description."""
        name = prior.name if prior is not None else resource_name(t.id, "project", s.key)
        description = s.description or ""
        body: JsonObj = {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "AppProject",
            "metadata": {"name": name, "labels": {MANAGED_BY_LABEL: MANAGED_BY}},
            "spec": {"description": description, "sourceRepos": [], "destinations": []},
        }
        path = f"{self._projects()}/{seg(name)}"
        async with self._client() as c:
            found = await c.call("GET", path, accept=NOT_FOUND)
            if found.status_code == HTTP_NOT_FOUND:
                post = await c.call("POST", self._projects(), json=body, accept=CONFLICT)
                if post.status_code == HTTP_CONFLICT:
                    # Created meanwhile; the retry re-reads it and checks that it is ours.
                    raise AdapterError(True, "the AppProject was created concurrently")
                state = ChangeState.CREATED
            else:
                existing = json_object(found)
                if not _marked(existing):
                    raise _collision("the AppProject")
                spec = existing.get("spec") or {}
                if (spec.get("description") or "") == description:
                    state = ChangeState.UNCHANGED
                else:
                    updated = {**existing, "spec": {**spec, "description": description}}
                    await self._put_project(c, path, updated)
                    state = ChangeState.UPDATED
        return Applied(Change(state), ResourceState(name, {"kind": "project", "name": name}))

    @staticmethod
    async def _put_project(c: KubeClient, path: str, project: JsonObj) -> None:
        """Replace a project; a 409 means it changed since it was read (the retry re-reads)."""
        put = await c.call("PUT", path, json=project, accept=CONFLICT)
        if put.status_code == HTTP_CONFLICT:
            raise AdapterError(True, "the AppProject changed concurrently")

    # ---- repositories ----------------------------------------------------
    async def ensure_repository(
        self, p: ProjectRef, s: RepoSpec, prior: ResourceState | None
    ) -> Applied:
        """Allow the repository's Git URL in the AppProject's `sourceRepos`; `key` is its name."""
        del prior
        url = self._repo_url(p.tenant, s.key)
        path = f"{self._projects()}/{seg(p.project.name)}"
        async with self._client() as c:
            project = await self._existing(c, path, "the AppProject", managed=True)
            repos = list((project.get("spec") or {}).get("sourceRepos") or [])
            if url in repos:
                state = ChangeState.UNCHANGED
            else:
                spec = {**(project.get("spec") or {}), "sourceRepos": [*repos, url]}
                await self._put_project(c, path, {**project, "spec": spec})
                state = ChangeState.UPDATED
        ref = {"kind": "repository", "project": p.project.name, "url": url}
        return Applied(Change(state), ResourceState(url, ref))

    @staticmethod
    async def _existing(
        c: KubeClient, path: str, what: str, *, managed: bool = False
    ) -> JsonObj:
        """GET an object that must exist; with `managed`, it must also be one we created."""
        found = await c.call("GET", path, accept=NOT_FOUND)
        if found.status_code == HTTP_NOT_FOUND:
            raise AdapterError(False, f"{what} does not exist in the Argo CD namespace")
        obj = json_object(found)
        if managed and not _marked(obj):
            raise _collision(what)
        return obj

    # ---- webhooks --------------------------------------------------------
    async def ensure_webhook(self, t: WebhookTarget, prior: ResourceState | None) -> Applied:
        """Write the notifications webhook service and its bearer token (`t.events` is unused)."""
        token = self._webhook_secret(t)
        name = prior.name if prior is not None else resource_name(t.tenant.id, "webhook", t.url)
        cm_key, token_key = f"service.webhook.{name}", f"{name}-token"
        service = _webhook_service(t.url, token_key)
        async with self._client() as c:
            cm_path = f"{self._core('configmaps')}/{NOTIFICATIONS_CM}"
            secret_path = f"{self._core('secrets')}/{NOTIFICATIONS_SECRET}"
            cm = await self._existing(c, cm_path, "argocd-notifications-cm")
            secret = await self._existing(c, secret_path, "argocd-notifications-secret")
            have_service = (cm.get("data") or {}).get(cm_key)
            have_token = _decoded(secret, token_key)
            if have_service == service and have_token == token.get_secret_value():
                state = ChangeState.UNCHANGED
            else:
                # The token first, so the service never references a key that is not there.
                patch = {"stringData": {token_key: token.get_secret_value()}}
                await c.call("PATCH", secret_path, json=patch)
                patch = {"data": {cm_key: service}}
                await c.call("PATCH", cm_path, json=patch)
                state = ChangeState.UPDATED if have_service is not None else ChangeState.CREATED
        return Applied(Change(state), ResourceState(name, {"kind": "webhook", "name": name}))

    def _webhook_secret(self, t: WebhookTarget) -> SecretStr:
        if t.secret_ref is None:
            raise AdapterConfigurationError("Argo CD webhooks need a token: set a secret_ref")
        secret = self._config.connection.get(t.secret_ref)
        if secret is None:
            raise AdapterConfigurationError("the webhook secret is missing from connection data")
        return secret

    # ---- deleting resources ----------------------------------------------
    async def delete(self, prior: ResourceState) -> Applied:
        """Delete a project, a repository entry or a webhook; absent is UNCHANGED."""
        kind = prior.ref.get("kind")
        async with self._client() as c:
            if kind == "project":
                path = f"{self._projects()}/{seg(prior.name)}"
                removed = await self._delete_managed(c, path)
            elif kind == "repository":
                removed = await self._remove_repository(c, prior)
            elif kind == "webhook":
                removed = await self._remove_webhook(c, prior.name)
            else:
                raise AdapterError(False, "unknown resource kind in state")
        state = ChangeState.DELETED if removed else ChangeState.UNCHANGED
        return Applied(Change(state), None)

    async def _remove_repository(self, c: KubeClient, prior: ResourceState) -> bool:
        path = f"{self._projects()}/{seg(prior.ref.get('project', ''))}"
        found = await c.call("GET", path, accept=NOT_FOUND)
        if found.status_code == HTTP_NOT_FOUND:
            return False
        project = json_object(found)
        spec = project.get("spec") or {}
        repos = list(spec.get("sourceRepos") or [])
        if not _marked(project) or prior.name not in repos:
            return False
        kept = [repo for repo in repos if repo != prior.name]
        await self._put_project(c, path, {**project, "spec": {**spec, "sourceRepos": kept}})
        return True

    @staticmethod
    async def _delete_managed(c: KubeClient, path: str) -> bool:
        """Delete the object only when we created it; absent or foreign is left alone (False)."""
        found = await c.call("GET", path, accept=NOT_FOUND)
        if found.status_code == HTTP_NOT_FOUND or not _marked(json_object(found)):
            return False
        gone = await c.call("DELETE", path, accept=NOT_FOUND)
        return gone.status_code != HTTP_NOT_FOUND

    async def _remove_webhook(self, c: KubeClient, name: str) -> bool:
        removed = False
        targets = (
            (f"{self._core('configmaps')}/{NOTIFICATIONS_CM}", f"service.webhook.{name}"),
            (f"{self._core('secrets')}/{NOTIFICATIONS_SECRET}", f"{name}-token"),
        )
        for path, key in targets:
            found = await c.call("GET", path, accept=NOT_FOUND)
            data = {} if found.status_code == HTTP_NOT_FOUND else json_object(found).get("data")
            if key in (data or {}):
                patch: JsonObj = {"data": {key: None}}
                await c.call("PATCH", path, json=patch)
                removed = True
        return removed

    # ---- credentials -----------------------------------------------------
    async def ensure_credential(
        self, s: CredentialScope, prior: CredentialState
    ) -> CredentialOutcome:
        """Write the repository (or repo-creds) Secret that carries the read-only App's key.

        With `platform_repository_key` set the Secret is a `repository` entry for exactly that
        repository; unset, it is a `repo-creds` template for every repository of the org.
        """
        slot = self._current(prior)
        app = self._github_app(s.tenant.github_host)
        if app is None:
            manual = Change(ChangeState.NEEDS_MANUAL_ACTION, manual_action=INSTALL_APP_ACTION)
            return CredentialOutcome(manual, prior, None)
        kind = "repo-creds" if s.platform_repository_key is None else "repository"
        fields = {"type": "git", "url": self._repo_url(s.tenant, s.platform_repository_key), **app}
        body: JsonObj = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": slot.name,
                "labels": {SECRET_TYPE_LABEL: kind, MANAGED_BY_LABEL: MANAGED_BY},
            },
            "type": "Opaque",
            "stringData": fields,
        }
        path = f"{self._core('secrets')}/{seg(slot.name)}"
        async with self._client() as c:
            found = await c.call("GET", path, accept=NOT_FOUND)
            if found.status_code == HTTP_NOT_FOUND:
                post = await c.call("POST", self._core("secrets"), json=body, accept=CONFLICT)
                if post.status_code == HTTP_CONFLICT:
                    # Created meanwhile; the retry re-reads it and checks that it is ours.
                    raise AdapterError(True, "the Secret was created concurrently")
                state = ChangeState.CREATED
            else:
                existing = json_object(found)
                if not _marked(existing):
                    raise _collision("the Secret")
                if self._secret_matches(existing, kind, fields):
                    state = ChangeState.UNCHANGED
                else:
                    await c.call("PUT", path, json=body)
                    state = ChangeState.UPDATED
        ref = {"kind": "secret", "secret": slot.name}
        new_state = CredentialState(
            current=replace(slot, ref=ref, delivered=True), previous=prior.previous
        )
        return CredentialOutcome(Change(state), new_state, NoCiCredential(NO_CI_REASON))

    def _github_app(self, host: GitHubHost) -> dict[str, str] | None:
        """Return the Secret fields of the read-only App, or None while it is not installed."""
        conn = self._config.connection
        app_id, key = conn.get("app_id"), conn.get("private_key")
        if app_id is None or key is None:
            raise AdapterConfigurationError("Argo CD connection data has no App id or private key")
        installation = conn.get("installation_id")
        if installation is None:
            return None
        fields = {
            "githubAppID": app_id.get_secret_value(),
            "githubAppInstallationID": installation.get_secret_value(),
            "githubAppPrivateKey": key.get_secret_value(),
        }
        if host.kind != "github.com":
            fields["githubAppEnterpriseBaseUrl"] = host.api_url
        return fields

    @staticmethod
    def _secret_matches(existing: JsonObj, kind: str, fields: dict[str, str]) -> bool:
        labels = (existing.get("metadata") or {}).get("labels") or {}
        stored = {key: _decoded(existing, key) for key in (existing.get("data") or {})}
        return bool(labels.get(SECRET_TYPE_LABEL) == kind and stored == fields)

    @staticmethod
    def _current(prior: CredentialState) -> CredentialSlot:
        if prior.current is None:
            raise AdapterError(False, "no current slot")
        return prior.current

    async def rotate_begin(self, s: CredentialScope, prior: CredentialState) -> CredentialOutcome:
        """Refuse: rotation is `none`; the Secret holds the App's key, which GitHub rotates."""
        del s, prior
        raise AdapterError(False, "Argo CD credentials do not rotate")

    async def rotate_finish(self, s: CredentialScope, prior: CredentialState) -> CredentialOutcome:
        """Delete `previous` only; idempotent."""
        del s
        removed = False
        if prior.previous is not None:
            async with self._client() as c:
                removed = await self._delete_secret(c, prior.previous)
        change = Change(ChangeState.DELETED if removed else ChangeState.UNCHANGED)
        return CredentialOutcome(change, CredentialState(current=prior.current), None)

    async def delete_credential(
        self, s: CredentialScope, prior: CredentialState
    ) -> CredentialOutcome:
        """Delete the Secrets of `current` and `previous`; absent is UNCHANGED."""
        del s
        removed = False
        async with self._client() as c:
            for slot in (prior.current, prior.previous):
                if slot is not None and await self._delete_secret(c, slot):
                    removed = True
        change = Change(ChangeState.DELETED if removed else ChangeState.UNCHANGED)
        return CredentialOutcome(change, CredentialState(current=None), None)

    async def _delete_secret(self, c: KubeClient, slot: CredentialSlot) -> bool:
        path = f"{self._core('secrets')}/{seg(slot.name)}"
        return await self._delete_managed(c, path)

    async def list_credentials(self, t: Tenant) -> Sequence[ListedCredential]:
        """List the tenant's trustbridge-named Secrets (found by label, filtered by name prefix)."""
        prefix = tenant_prefix(t.id)
        params = {"labelSelector": f"{MANAGED_BY_LABEL}={MANAGED_BY},{SECRET_TYPE_LABEL}"}
        async with self._client() as c:
            listing = await c.call("GET", self._core("secrets"), params=params)
        names = [
            str((item.get("metadata") or {}).get("name") or "")
            for item in json_object(listing).get("items") or []
            if isinstance(item, dict)
        ]
        return [
            ListedCredential(name, {"kind": "secret", "secret": name})
            for name in names
            if name.startswith(prefix)
        ]

    def github_outputs(
        self, s: CredentialScope, credential: OidcTrust | IssuedSecret | NoCiCredential
    ) -> Sequence[GitHubOutput]:
        """Return nothing: Argo CD is pull-based, so no CI variable or secret exists."""
        del s, credential
        return []

    async def health_check(self) -> Health:
        """Read one AppProject; this proves the URL, the token, the RBAC and the CRD at once."""
        try:
            async with self._client() as c:
                await c.call("GET", self._projects(), params={"limit": "1"})
        except AdapterError as exc:
            return Health(ok=False, detail=exc.message)
        return Health(ok=True)
