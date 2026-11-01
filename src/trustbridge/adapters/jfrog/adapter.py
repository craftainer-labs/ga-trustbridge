"""The JFrog Artifactory adapter (NG5a, ADR 0029).

See `README.md` for the endpoints, which payload shapes the JFrog documentation confirms, and
which still need a live confirmation.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import ClassVar
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import SecretStr

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
    Permission,
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
from trustbridge.adapters.jfrog.client import JFrogClient, JsonObj, json_object, seg

ACCESS = "/access/api/v1"
ARTIFACTORY = "/artifactory/api"
EVENTS = "/event/api/v1"

HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
NOT_FOUND = frozenset({HTTP_NOT_FOUND})
ABSENT_REPO = frozenset({400, HTTP_NOT_FOUND})
CONFLICT = frozenset({HTTP_CONFLICT})

DEFAULT_PROJECT = "default"
"""The spec key of the tenant project that credentials target (unless `project_key` is set)."""

AUDIENCE = "jfrog-github"
"""The audience GitHub workflows request and the JFrog OIDC integration expects."""

WEBHOOK_HEADER = "X-JFrog-Event-Auth"
DEFAULT_WEBHOOK_EVENTS = ("pushed",)
TOKEN_TTL_SECONDS = 90 * 24 * 3600
OIDC_TOKEN_TTL_SECONDS = 3600
PROJECT_DIGEST_LENGTH = 20
MAX_REPO_SLUG = 41

# JFrog resolves identity mappings by priority (the lowest number wins), so the strongest
# permission gets the lowest number: a workflow in a protected environment that matches both the
# `push` and the `pull` mapping receives the `push` token. Org-wide mappings rank below repository
# ones. This is the selection rule ADR 0029's open issue asks the fixtures to show.
PRIORITY: dict[Permission, int] = {"admin": 10, "push": 20, "pull": 30}
ORG_WIDE_PRIORITY_OFFSET = 5
ROLES: dict[Permission, str] = {"pull": "Viewer", "push": "Developer", "admin": "Project Admin"}

_SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")


def project_key_for(tenant_id: UUID, key: str) -> str:
    """Return the JFrog project key for a tenant's project spec (lowercase alphanumerics)."""
    digest = sha256(f"{tenant_id.hex}|{key}".encode()).hexdigest()[:PROJECT_DIGEST_LENGTH]
    return f"tb{digest}"


def provider_name(tenant_id: UUID) -> str:
    """Return the name of the tenant's OIDC integration on JFrog."""
    return f"{tenant_prefix(tenant_id)}oidc"


def trust_claims(scope: CredentialScope) -> dict[str, str]:
    """Return the claims an identity mapping requires (invariant A6; strings, no `sub`)."""
    claims = {"repository_owner_id": str(scope.repository_owner_id)}
    if scope.repository_id is not None:
        claims["repository_id"] = str(scope.repository_id)
    if scope.environment is not None:
        claims["environment"] = scope.environment
    return claims


def mapping_priority(scope: CredentialScope) -> int:
    """Return the identity mapping priority: strongest permission first, repository before org."""
    offset = ORG_WIDE_PRIORITY_OFFSET if scope.repository_id is None else 0
    return PRIORITY[scope.permission] + offset


def _kind_of_name(name: str) -> str:
    """Infer a resource kind from a deterministic name when its ref is still empty."""
    if name.startswith("tb-"):
        return "webhook"
    return "repository" if "-" in name else "project"


class JFrogAdapter:
    """Provisions JFrog projects, repositories, OIDC identity mappings and fallback tokens."""

    name: ClassVar[str] = "jfrog"
    api_version: ClassVar[int] = API_VERSION

    def __init__(
        self,
        config: AdapterConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Keep the per-tenant config; `transport` and `clock` exist for the contract tests."""
        self._config = config
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(UTC))

    # ---- configuration ---------------------------------------------------
    def capabilities(self) -> Capabilities:
        """Describe JFrog: OIDC on every host kind, single-step rotation, HMAC webhooks."""
        kinds: frozenset[HostKind] = frozenset({"github.com", "ghes", "ghe.com"})
        return Capabilities(
            oidc=True,
            oidc_hosts=kinds,
            oidc_requires_manual_step=False,
            ci_credential=True,
            rotation="single",
            webhook=WebhookCapability(WebhookAuth.HMAC, WEBHOOK_HEADER, trigger_only=True),
            github_hosts=kinds,
            needs_privileged_credential=True,
        )

    def _base_url(self) -> str:
        raw = self._config.connection.get("base_url")
        if raw is None:
            raise AdapterConfigurationError("JFrog connection data has no base_url")
        url = raw.get_secret_value().rstrip("/")
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.netloc or parts.query or parts.fragment:
            raise AdapterConfigurationError("JFrog base_url must be an https URL without a query")
        return url

    def _client(self) -> JFrogClient:
        token = self._config.privileged_credential
        if token is None:
            raise AdapterConfigurationError("JFrog admin access token is not configured")
        return JFrogClient(self._base_url(), token, self._transport)

    def _project_for_credentials(self) -> str:
        override = self._config.connection.get("project_key")
        if override is not None:
            return override.get_secret_value()
        return project_key_for(self._config.tenant.id, DEFAULT_PROJECT)

    # ---- projects --------------------------------------------------------
    async def ensure_project(
        self, t: Tenant, s: ProjectSpec, prior: ResourceState | None
    ) -> Applied:
        """Create the project, or update its description when it drifted."""
        key = prior.name if prior is not None else project_key_for(t.id, s.key)
        description = s.description or ""
        body: JsonObj = {"project_key": key, "display_name": key, "description": description}
        path = f"{ACCESS}/projects/{seg(key)}"
        async with self._client() as c:
            found = await c.call("GET", path, accept=NOT_FOUND)
            if found.status_code == HTTP_NOT_FOUND:
                created = await c.call("POST", f"{ACCESS}/projects", json=body, accept=CONFLICT)
                state = (
                    ChangeState.UNCHANGED
                    if created.status_code == HTTP_CONFLICT
                    else ChangeState.CREATED
                )
            else:
                existing = json_object(found)
                if (existing.get("description") or "") == description:
                    state = ChangeState.UNCHANGED
                else:
                    await c.call("PUT", path, json={**existing, "description": description})
                    state = ChangeState.UPDATED
        ref = {"kind": "project", "project_key": key}
        return Applied(Change(state), ResourceState(name=key, ref=ref))

    # ---- repositories ----------------------------------------------------
    async def ensure_repository(
        self, p: ProjectRef, s: RepoSpec, prior: ResourceState | None
    ) -> Applied:
        """Create the project repository; a different package type is a non-retryable error."""
        project_key = p.project.ref.get("project_key", p.project.name)
        key = prior.name if prior is not None else self._repo_key(project_key, s.key)
        package_type = s.package_type.lower()
        body: JsonObj = {
            "key": key,
            "projectKey": project_key,
            "rclass": "local",
            "packageType": package_type,
            "description": "managed by trustbridge",
        }
        if package_type == "docker":
            body["dockerApiVersion"] = "V2"
        path = f"{ARTIFACTORY}/repositories/{seg(key)}"
        async with self._client() as c:
            found = await c.call("GET", path, accept=ABSENT_REPO)
            if found.status_code in ABSENT_REPO:
                put = await c.call("PUT", path, json=body, accept=ABSENT_REPO | CONFLICT)
                if put.status_code in ABSENT_REPO | CONFLICT:
                    # Lost a race (or the key exists case-insensitively): converge on what is there.
                    found = await c.call("GET", path, accept=ABSENT_REPO)
                    if found.status_code in ABSENT_REPO:
                        msg = f"JFrog rejected creating repository (HTTP {put.status_code})"
                        raise AdapterError(False, msg)
                    state = ChangeState.UNCHANGED
                else:
                    state = ChangeState.CREATED
            else:
                state = ChangeState.UNCHANGED
            if state is ChangeState.UNCHANGED:
                self._check_repo(json_object(found), package_type, project_key)
        ref = {"kind": "repository", "repo_key": key, "project_key": project_key}
        return Applied(Change(state), ResourceState(name=key, ref=ref))

    @staticmethod
    def _repo_key(project_key: str, slug: str) -> str:
        if not _SLUG.fullmatch(slug) or len(slug) > MAX_REPO_SLUG:
            msg = "repository key must be lowercase letters, digits and '-' (at most 41 characters)"
            raise AdapterConfigurationError(msg)
        return f"{project_key}-{slug}"

    @staticmethod
    def _check_repo(existing: JsonObj, package_type: str, project_key: str) -> None:
        if str(existing.get("packageType", package_type)).lower() != package_type:
            raise AdapterError(False, "repository exists with a different package type")
        if existing.get("projectKey", project_key) != project_key:
            raise AdapterError(False, "repository exists in a different project")

    # ---- webhooks --------------------------------------------------------
    async def ensure_webhook(self, t: WebhookTarget, prior: ResourceState | None) -> Applied:
        """Create the signed webhook subscription, or update it when URL or events drifted."""
        secret = self._webhook_secret(t)
        key = prior.name if prior is not None else resource_name(t.tenant.id, "webhook", t.url)
        events = list(t.events or DEFAULT_WEBHOOK_EVENTS)
        body: JsonObj = {
            "key": key,
            "description": "managed by trustbridge",
            "enabled": True,
            "event_filter": {
                "domain": "docker",
                "event_types": events,
                "criteria": {
                    "anyLocal": True,
                    "anyRemote": False,
                    "anyFederated": False,
                    "repoKeys": [],
                },
            },
            "handlers": [
                {
                    "handler_type": "webhook",
                    "url": t.url,
                    "secret": secret.get_secret_value(),
                    "use_secret_for_signing": True,
                }
            ],
        }
        path = f"{EVENTS}/subscriptions/{seg(key)}"
        async with self._client() as c:
            found = await c.call("GET", path, accept=NOT_FOUND)
            if found.status_code == HTTP_NOT_FOUND:
                post = await c.call("POST", f"{EVENTS}/subscriptions", json=body, accept=CONFLICT)
                state = (
                    ChangeState.UNCHANGED
                    if post.status_code == HTTP_CONFLICT
                    else ChangeState.CREATED
                )
            elif self._webhook_matches(json_object(found), t.url, events):
                state = ChangeState.UNCHANGED
            else:
                await c.call("PUT", path, json=body)
                state = ChangeState.UPDATED
        return Applied(Change(state), ResourceState(name=key, ref={"kind": "webhook", "key": key}))

    def _webhook_secret(self, t: WebhookTarget) -> SecretStr:
        if t.secret_ref is None:
            raise AdapterConfigurationError("JFrog webhooks must be signed: set a secret_ref")
        secret = self._config.connection.get(t.secret_ref)
        if secret is None:
            raise AdapterConfigurationError("the webhook secret is missing from connection data")
        return secret

    @staticmethod
    def _webhook_matches(existing: JsonObj, url: str, events: list[str]) -> bool:
        handlers = existing.get("handlers") or [{}]
        event_filter = existing.get("event_filter") or {}
        return (
            handlers[0].get("url") == url
            and sorted(event_filter.get("event_types") or []) == sorted(events)
            and existing.get("enabled", True) is True
        )

    # ---- deleting resources ----------------------------------------------
    async def delete(self, prior: ResourceState) -> Applied:
        """Delete a project, repository or webhook; absent is UNCHANGED."""
        kind = prior.ref.get("kind") or _kind_of_name(prior.name)
        paths = {
            "project": f"{ACCESS}/projects/{seg(prior.name)}",
            "repository": f"{ARTIFACTORY}/repositories/{seg(prior.name)}",
            "webhook": f"{EVENTS}/subscriptions/{seg(prior.name)}",
        }
        if kind not in paths:
            raise AdapterError(False, "unknown resource kind in state")
        async with self._client() as c:
            gone = await c.call("DELETE", paths[kind], accept=NOT_FOUND)
        state = ChangeState.UNCHANGED if gone.status_code == HTTP_NOT_FOUND else ChangeState.DELETED
        return Applied(Change(state), None)

    # ---- credentials -----------------------------------------------------
    async def ensure_credential(
        self, s: CredentialScope, prior: CredentialState
    ) -> CredentialOutcome:
        """Establish the identity mapping (when `oidc_eligible`) or issue an access token."""
        slot = self._current(prior)
        async with self._client() as c:
            if s.oidc_eligible:
                return await self._ensure_trust(c, s, prior, slot)
            existing = await self._find_tokens(c, slot.name)
            if slot.delivered and existing:
                return CredentialOutcome(Change(ChangeState.UNCHANGED), prior, None)
            return await self._issue_token(c, s, prior, existing)

    async def rotate_begin(self, s: CredentialScope, prior: CredentialState) -> CredentialOutcome:
        """Replace the token under the same name in place (rotation is `single`)."""
        slot = self._current(prior)
        if slot.ref.get("kind") == "mapping":
            raise AdapterError(False, "an OIDC trust has no secret to rotate")
        async with self._client() as c:
            existing = await self._find_tokens(c, slot.name)
            return await self._issue_token(c, s, replace(prior, previous=None), existing)

    async def rotate_finish(self, s: CredentialScope, prior: CredentialState) -> CredentialOutcome:
        """Delete `previous` only; idempotent."""
        del s
        removed = False
        if prior.previous is not None:
            async with self._client() as c:
                removed = await self._delete_slot(c, prior.previous)
        change = Change(ChangeState.DELETED if removed else ChangeState.UNCHANGED)
        return CredentialOutcome(change, CredentialState(current=prior.current), None)

    async def delete_credential(
        self, s: CredentialScope, prior: CredentialState
    ) -> CredentialOutcome:
        """Delete the tokens and mappings of `current` and `previous`; absent is UNCHANGED."""
        removed = False
        async with self._client() as c:
            for slot in (prior.current, prior.previous):
                if slot is not None and await self._delete_slot(c, slot, s.tenant):
                    removed = True
        change = Change(ChangeState.DELETED if removed else ChangeState.UNCHANGED)
        return CredentialOutcome(change, CredentialState(current=None), None)

    async def list_credentials(self, t: Tenant) -> Sequence[ListedCredential]:
        """List the tenant's trustbridge-named tokens and identity mappings."""
        prefix = tenant_prefix(t.id)
        provider = provider_name(t.id)
        async with self._client() as c:
            tokens = await self._find_tokens(c, prefix, prefix=True)
            listed = [
                ListedCredential(
                    str(tok["description"]),
                    {"kind": "token", "token_id": str(tok["token_id"])},
                )
                for tok in tokens
            ]
            found = await c.call(
                "GET", f"{ACCESS}/oidc/{seg(provider)}/identity_mappings", accept=NOT_FOUND
            )
            if found.status_code != HTTP_NOT_FOUND:
                for mapping in self._mapping_items(found):
                    name = str(mapping.get("name", ""))
                    if name.startswith(prefix):
                        ref = {"kind": "mapping", "provider": provider, "mapping": name}
                        listed.append(ListedCredential(name, ref))
        return listed

    def github_outputs(
        self, s: CredentialScope, credential: OidcTrust | IssuedSecret | NoCiCredential
    ) -> Sequence[GitHubOutput]:
        """Return the JFrog URL, plus the OIDC provider or the token (a pure function)."""
        if isinstance(credential, NoCiCredential):
            return []
        repo, env = s.repository_id, s.environment
        outputs = [GitHubOutput("variable", "JFROG_URL", self._base_url(), repo, env)]
        if isinstance(credential, OidcTrust):
            outputs.append(
                GitHubOutput(
                    "variable", "JFROG_OIDC_PROVIDER", provider_name(s.tenant.id), repo, env
                )
            )
            outputs.append(
                GitHubOutput(
                    "variable", "JFROG_OIDC_AUDIENCE", credential.audience or AUDIENCE, repo, env
                )
            )
        else:
            outputs.append(
                GitHubOutput("secret", "JFROG_ACCESS_TOKEN", credential.values["token"], repo, env)
            )
        return outputs

    async def health_check(self) -> Health:
        """Ping the Access service; configuration and transport problems report `ok=False`."""
        try:
            async with self._client() as c:
                await c.call("GET", f"{ACCESS}/system/ping")
        except AdapterError as exc:
            return Health(ok=False, detail=exc.message)
        return Health(ok=True)

    # ---- trust -----------------------------------------------------------
    async def _ensure_trust(
        self, c: JFrogClient, s: CredentialScope, prior: CredentialState, slot: CredentialSlot
    ) -> CredentialOutcome:
        provider = provider_name(s.tenant.id)
        provider_changed = await self._ensure_provider(c, s, provider)
        desired = self._mapping_body(s, slot, provider)
        path = f"{ACCESS}/oidc/{seg(provider)}/identity_mappings"
        found = await c.call("GET", f"{path}/{seg(slot.name)}", accept=NOT_FOUND)
        if found.status_code == HTTP_NOT_FOUND:
            post = await c.call("POST", path, json=desired, accept=CONFLICT)
            state = (
                ChangeState.UNCHANGED if post.status_code == HTTP_CONFLICT else ChangeState.CREATED
            )
        elif self._mapping_matches(json_object(found), desired):
            state = ChangeState.UPDATED if provider_changed else ChangeState.UNCHANGED
        else:
            await c.call("PUT", f"{path}/{seg(slot.name)}", json=desired)
            state = ChangeState.UPDATED
        ref = {"kind": "mapping", "provider": provider, "mapping": slot.name}
        new_state = CredentialState(
            current=replace(slot, ref=ref, delivered=True), previous=prior.previous
        )
        trust = OidcTrust(s.github_host.oidc_issuer, AUDIENCE, trust_claims(s))
        return CredentialOutcome(Change(state), new_state, trust)

    async def _ensure_provider(self, c: JFrogClient, s: CredentialScope, provider: str) -> bool:
        desired = self._provider_body(s.tenant, s.github_host, provider)
        path = f"{ACCESS}/oidc/{seg(provider)}"
        found = await c.call("GET", path, accept=NOT_FOUND)
        if found.status_code == HTTP_NOT_FOUND:
            post = await c.call("POST", f"{ACCESS}/oidc", json=desired, accept=CONFLICT)
            return post.status_code != HTTP_CONFLICT
        existing = json_object(found)
        if (
            existing.get("issuer_url") == desired["issuer_url"]
            and existing.get("audience") == AUDIENCE
        ):
            return False
        await c.call("PUT", path, json=desired)
        return True

    @staticmethod
    def _provider_body(tenant: Tenant, host: GitHubHost, provider: str) -> JsonObj:
        body: JsonObj = {
            "name": provider,
            "description": "managed by trustbridge",
            "issuer_url": host.oidc_issuer,
            "audience": AUDIENCE,
        }
        if host.kind == "github.com":
            body["provider_type"] = "GitHub"
            body["organization"] = tenant.org
        else:
            body["provider_type"] = "Generic OpenID Connect"
            body["token_issuer"] = host.oidc_issuer
        return body

    def _mapping_body(self, s: CredentialScope, slot: CredentialSlot, provider: str) -> JsonObj:
        project = self._project_for_credentials()
        scope = f"applied-permissions/roles:{project}:{ROLES[s.permission]}"
        return {
            "name": slot.name,
            "description": "managed by trustbridge",
            "provider_name": provider,
            "project_key": project,
            "priority": mapping_priority(s),
            "claims": trust_claims(s),
            "token_spec": {
                "username": slot.name,
                "scope": scope,
                "audience": "*@*",
                "expires_in": OIDC_TOKEN_TTL_SECONDS,
            },
        }

    @staticmethod
    def _mapping_matches(existing: JsonObj, desired: JsonObj) -> bool:
        spec = existing.get("token_spec") or {}
        return bool(
            existing.get("claims") == desired["claims"]
            and existing.get("priority") == desired["priority"]
            and spec.get("scope") == desired["token_spec"]["scope"]
        )

    @staticmethod
    def _mapping_items(response: httpx.Response) -> list[JsonObj]:
        data = response.json()
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), [])
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    # ---- tokens ----------------------------------------------------------
    @staticmethod
    def _current(prior: CredentialState) -> CredentialSlot:
        if prior.current is None:
            raise AdapterError(False, "no current slot")
        return prior.current

    @staticmethod
    async def _find_tokens(c: JFrogClient, name: str, *, prefix: bool = False) -> list[JsonObj]:
        tokens = json_object(await c.call("GET", f"{ACCESS}/tokens")).get("tokens") or []
        found = []
        for token in tokens:
            description = str(token.get("description") or "")
            if description.startswith(name) if prefix else description == name:
                found.append(token)
        return found

    async def _issue_token(
        self,
        c: JFrogClient,
        s: CredentialScope,
        base: CredentialState,
        existing: list[JsonObj],
    ) -> CredentialOutcome:
        """Create the new token first, then revoke older ones under the same name (A2, A3)."""
        slot = self._current(base)
        project = self._project_for_credentials()
        body: JsonObj = {
            "username": slot.name,
            "description": slot.name,
            "scope": f"applied-permissions/roles:{project}:{ROLES[s.permission]}",
            "project_key": project,
            "expires_in": TOKEN_TTL_SECONDS,
            "refreshable": False,
        }
        created = json_object(await c.call("POST", f"{ACCESS}/tokens", json=body))
        access_token = created.get("access_token")
        token_id = created.get("token_id")
        if not isinstance(access_token, str) or not isinstance(token_id, str):
            raise AdapterError(False, "JFrog returned an unexpected token response")
        for old in existing:
            await c.call("DELETE", f"{ACCESS}/tokens/{seg(str(old['token_id']))}", accept=NOT_FOUND)
        ttl = int(created.get("expires_in") or TOKEN_TTL_SECONDS)
        issued = IssuedSecret(
            {"token": SecretStr(access_token)}, self._clock() + timedelta(seconds=ttl)
        )
        ref = {"kind": "token", "token_id": token_id}
        state = CredentialState(
            current=replace(slot, ref=ref, delivered=False), previous=base.previous
        )
        change = Change(ChangeState.UPDATED if existing else ChangeState.CREATED)
        return CredentialOutcome(change, state, issued)

    async def _delete_slot(
        self, c: JFrogClient, slot: CredentialSlot, tenant: Tenant | None = None
    ) -> bool:
        """Delete what a slot names: tokens by description, a mapping by name. True if any went."""
        kind = slot.ref.get("kind")
        removed = False
        if kind != "mapping":
            for token in await self._find_tokens(c, slot.name):
                path = f"{ACCESS}/tokens/{seg(str(token['token_id']))}"
                removed |= (
                    await c.call("DELETE", path, accept=NOT_FOUND)
                ).status_code != HTTP_NOT_FOUND
        if kind != "token":
            provider = slot.ref.get("provider") or provider_name((tenant or self._config.tenant).id)
            path = f"{ACCESS}/oidc/{seg(provider)}/identity_mappings/{seg(slot.name)}"
            removed |= (
                await c.call("DELETE", path, accept=NOT_FOUND)
            ).status_code != HTTP_NOT_FOUND
        return removed
