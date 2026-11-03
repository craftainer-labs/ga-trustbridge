"""The Harbor / Mirantis Secure Registry adapter (NG5b, ADR 0029).

See `README.md` for the endpoints, which payload shapes the Harbor documentation confirms, which
still need a live confirmation, and the known limitations (notably: no per-repository grants).
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

from trustbridge.adapters.harbor.client import (
    HarborClient,
    JsonObj,
    json_list,
    json_object,
    seg,
)
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

API = "/api/v2.0"

HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
NOT_FOUND = frozenset({HTTP_NOT_FOUND})
CONFLICT = frozenset({HTTP_CONFLICT})

DEFAULT_PROJECT = "default"
"""The spec key of the tenant project that credentials target (unless `project_key` is set)."""

WEBHOOK_HEADER = "Authorization"
DEFAULT_WEBHOOK_EVENTS = ("PUSH_ARTIFACT",)
ROBOT_DURATION_DAYS = 90
PROJECT_DIGEST_LENGTH = 20
PAGE_SIZE = "100"
MAX_REPO_PATH = 200

# Harbor robot permissions stop at project scope (the `repository` resource of a project): there
# is no per-repository grant. pull is read, push includes pull, admin adds delete.
ACTIONS: dict[Permission, tuple[str, ...]] = {
    "pull": ("pull",),
    "push": ("pull", "push"),
    "admin": ("pull", "push", "delete"),
}

_REPO_PATH = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*")


def project_name_for(tenant_id: UUID, key: str) -> str:
    """Return the Harbor project name for a tenant's project spec (lowercase alphanumerics)."""
    digest = sha256(f"{tenant_id.hex}|{key}".encode()).hexdigest()[:PROJECT_DIGEST_LENGTH]
    return f"tb{digest}"


def robot_access(permission: Permission) -> list[JsonObj]:
    """Return the Harbor `access` entries of a project robot for a permission."""
    return [
        {"resource": "repository", "action": action, "effect": "allow"}
        for action in ACTIONS[permission]
    ]


class HarborAdapter:
    """Provisions Harbor / MSR projects, project robot accounts and webhook policies."""

    name: ClassVar[str] = "harbor"
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
        """Describe Harbor: secrets only (no OIDC trust), in-place rotation, static-header hooks."""
        kinds: frozenset[HostKind] = frozenset({"github.com", "ghes", "ghe.com"})
        return Capabilities(
            oidc=False,
            oidc_hosts=frozenset(),
            oidc_requires_manual_step=False,
            ci_credential=True,
            rotation="single",
            webhook=WebhookCapability(WebhookAuth.STATIC_HEADER, WEBHOOK_HEADER, trigger_only=True),
            github_hosts=kinds,
            needs_privileged_credential=True,
        )

    def _base_url(self) -> str:
        raw = self._config.connection.get("base_url")
        if raw is None:
            raise AdapterConfigurationError("Harbor connection data has no base_url")
        url = raw.get_secret_value().rstrip("/")
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.netloc or parts.query or parts.fragment:
            raise AdapterConfigurationError("Harbor base_url must be an https URL without a query")
        return url

    def _client(self) -> HarborClient:
        password = self._config.privileged_credential
        if password is None:
            raise AdapterConfigurationError("Harbor admin credential is not configured")
        user = self._config.connection.get("username")
        if user is None:
            raise AdapterConfigurationError("Harbor connection data has no username")
        return HarborClient(self._base_url(), user.get_secret_value(), password, self._transport)

    def _project_for_credentials(self) -> str:
        override = self._config.connection.get("project_key")
        if override is not None:
            return override.get_secret_value()
        return project_name_for(self._config.tenant.id, DEFAULT_PROJECT)

    # ---- projects --------------------------------------------------------
    async def ensure_project(
        self, t: Tenant, s: ProjectSpec, prior: ResourceState | None
    ) -> Applied:
        """Create the private project when it is absent; an existing one is left as is."""
        name = prior.name if prior is not None else project_name_for(t.id, s.key)
        body: JsonObj = {"project_name": name, "metadata": {"public": "false"}}
        async with self._client() as c:
            found = await c.call("GET", f"{API}/projects/{seg(name)}", accept=NOT_FOUND)
            if found.status_code == HTTP_NOT_FOUND:
                created = await c.call("POST", f"{API}/projects", json=body, accept=CONFLICT)
                state = (
                    ChangeState.UNCHANGED
                    if created.status_code == HTTP_CONFLICT
                    else ChangeState.CREATED
                )
            else:
                state = ChangeState.UNCHANGED
        ref = {"kind": "project", "project_name": name}
        return Applied(Change(state), ResourceState(name=name, ref=ref))

    # ---- repositories ----------------------------------------------------
    async def ensure_repository(
        self, p: ProjectRef, s: RepoSpec, prior: ResourceState | None
    ) -> Applied:
        """Name the repository; Harbor has no create-repository API, so nothing is sent.

        A Harbor repository exists from its first push into the project. The returned state is
        the deterministic `<project>/<key>` name, and the change is always UNCHANGED.
        """
        project = p.project.ref.get("project_name", p.project.name)
        name = prior.name if prior is not None else self._repo_name(project, s.key)
        ref = {"kind": "repository", "repo_name": name, "project_name": project}
        return Applied(Change(ChangeState.UNCHANGED), ResourceState(name=name, ref=ref))

    @staticmethod
    def _repo_name(project: str, key: str) -> str:
        if not _REPO_PATH.fullmatch(key) or len(key) > MAX_REPO_PATH:
            msg = "repository key must be lowercase letters, digits, '.', '_', '-' and '/' paths"
            raise AdapterConfigurationError(msg)
        return f"{project}/{key}"

    # ---- webhooks --------------------------------------------------------
    async def ensure_webhook(self, t: WebhookTarget, prior: ResourceState | None) -> Applied:
        """Create the project webhook policy, or update it when URL, events or state drifted."""
        secret = self._webhook_secret(t)
        project = prior.ref.get("project") if prior is not None else None
        project = project or self._project_for_credentials()
        name = prior.name if prior is not None else resource_name(t.tenant.id, "webhook", t.url)
        events = list(t.events or DEFAULT_WEBHOOK_EVENTS)
        body: JsonObj = {
            "name": name,
            "description": "managed by trustbridge",
            "enabled": True,
            "event_types": events,
            "targets": [
                {
                    "type": "http",
                    "address": t.url,
                    "auth_header": secret.get_secret_value(),
                    "skip_cert_verify": False,
                    "payload_format": "Default",
                }
            ],
        }
        base = f"{API}/projects/{seg(project)}/webhook/policies"
        async with self._client() as c:
            existing = await self._find_policy(c, project, name)
            if existing is None:
                post = await c.call("POST", base, json=body, accept=CONFLICT)
                state = (
                    ChangeState.UNCHANGED
                    if post.status_code == HTTP_CONFLICT
                    else ChangeState.CREATED
                )
            elif self._webhook_matches(existing, t.url, events):
                state = ChangeState.UNCHANGED
            else:
                await c.call("PUT", f"{base}/{self._policy_id(existing)}", json=body)
                state = ChangeState.UPDATED
        ref = {"kind": "webhook", "project": project, "policy": name}
        return Applied(Change(state), ResourceState(name=name, ref=ref))

    def _webhook_secret(self, t: WebhookTarget) -> SecretStr:
        if t.secret_ref is None:
            raise AdapterConfigurationError("Harbor webhooks must carry a secret: set a secret_ref")
        secret = self._config.connection.get(t.secret_ref)
        if secret is None:
            raise AdapterConfigurationError("the webhook secret is missing from connection data")
        return secret

    @staticmethod
    def _policy_id(policy: JsonObj) -> str:
        policy_id = policy.get("id")
        if not isinstance(policy_id, int) or isinstance(policy_id, bool):
            raise AdapterError(False, "Harbor returned a webhook policy without an id")
        return str(policy_id)

    @staticmethod
    async def _find_policy(c: HarborClient, project: str, name: str) -> JsonObj | None:
        listing = await c.call(
            "GET",
            f"{API}/projects/{seg(project)}/webhook/policies",
            params={"q": f"name={name}", "page_size": PAGE_SIZE},
            accept=NOT_FOUND,
        )
        if listing.status_code == HTTP_NOT_FOUND:
            return None
        return next((p for p in json_list(listing) if p.get("name") == name), None)

    @staticmethod
    def _webhook_matches(existing: JsonObj, url: str, events: list[str]) -> bool:
        targets = existing.get("targets") or [{}]
        return bool(
            targets[0].get("address") == url
            and sorted(existing.get("event_types") or []) == sorted(events)
            and existing.get("enabled", True) is True
        )

    # ---- deleting resources ----------------------------------------------
    async def delete(self, prior: ResourceState) -> Applied:
        """Delete a project or webhook policy; absent is UNCHANGED. Repositories are not touched."""
        kind = prior.ref.get("kind")
        if kind == "repository":
            # Never created by the adapter, and deleting one would destroy its images.
            return Applied(Change(ChangeState.UNCHANGED), None)
        if kind == "webhook":
            return await self._delete_webhook(prior)
        if kind != "project":
            raise AdapterError(False, "unknown resource kind in state")
        async with self._client() as c:
            gone = await c.call("DELETE", f"{API}/projects/{seg(prior.name)}", accept=NOT_FOUND)
        state = ChangeState.UNCHANGED if gone.status_code == HTTP_NOT_FOUND else ChangeState.DELETED
        return Applied(Change(state), None)

    async def _delete_webhook(self, prior: ResourceState) -> Applied:
        project = prior.ref.get("project") or self._project_for_credentials()
        async with self._client() as c:
            existing = await self._find_policy(c, project, prior.name)
            if existing is None:
                return Applied(Change(ChangeState.UNCHANGED), None)
            path = f"{API}/projects/{seg(project)}/webhook/policies/{self._policy_id(existing)}"
            gone = await c.call("DELETE", path, accept=NOT_FOUND)
        state = ChangeState.UNCHANGED if gone.status_code == HTTP_NOT_FOUND else ChangeState.DELETED
        return Applied(Change(state), None)

    # ---- credentials -----------------------------------------------------
    async def ensure_credential(
        self, s: CredentialScope, prior: CredentialState
    ) -> CredentialOutcome:
        """Create the project robot account, or re-issue its secret when it was never delivered.

        `platform_repository_key` does not narrow the grant: Harbor has no per-repository
        permission, so the robot always covers the whole project (see the README).
        """
        slot = self._current(prior)
        project = self._project_for_credentials()
        async with self._client() as c:
            existing = await self._find_robot(c, project, slot.name)
            if existing is None:
                return await self._create_robot(c, s, prior, slot, project)
            if slot.delivered:
                return CredentialOutcome(Change(ChangeState.UNCHANGED), prior, None)
            return await self._refresh_robot(c, replace(prior, previous=None), slot, existing)

    async def rotate_begin(self, s: CredentialScope, prior: CredentialState) -> CredentialOutcome:
        """Replace the robot's secret in place (rotation is `single`)."""
        slot = self._current(prior)
        project = self._project_for_credentials()
        base = replace(prior, previous=None)
        async with self._client() as c:
            existing = await self._find_robot(c, project, slot.name)
            if existing is None:
                return await self._create_robot(c, s, base, slot, project)
            return await self._refresh_robot(c, base, slot, existing)

    async def rotate_finish(self, s: CredentialScope, prior: CredentialState) -> CredentialOutcome:
        """Delete `previous` only (never the robot `current` names); idempotent."""
        del s
        removed = False
        previous, current = prior.previous, prior.current
        if previous is not None and (current is None or previous.name != current.name):
            async with self._client() as c:
                removed = await self._delete_slot(c, previous)
        change = Change(ChangeState.DELETED if removed else ChangeState.UNCHANGED)
        return CredentialOutcome(change, CredentialState(current=prior.current), None)

    async def delete_credential(
        self, s: CredentialScope, prior: CredentialState
    ) -> CredentialOutcome:
        """Delete the robots of `current` and `previous`; absent is UNCHANGED."""
        del s
        removed = False
        async with self._client() as c:
            for slot in (prior.current, prior.previous):
                if slot is not None and await self._delete_slot(c, slot):
                    removed = True
        change = Change(ChangeState.DELETED if removed else ChangeState.UNCHANGED)
        return CredentialOutcome(change, CredentialState(current=None), None)

    async def list_credentials(self, t: Tenant) -> Sequence[ListedCredential]:
        """List the tenant's trustbridge-named robot accounts (all projects)."""
        prefix = tenant_prefix(t.id)
        async with self._client() as c:
            robots = await self._robots(c, prefix)
        listed = []
        for robot in robots:
            full = robot.get("name")
            if not isinstance(full, str) or "+" not in full:
                continue
            head, slot_name = full.rsplit("+", 1)
            if not slot_name.startswith(prefix):
                continue
            ref = {
                "kind": "robot",
                "robot_id": self._robot_id(robot),
                "project": head.rsplit("$", 1)[-1],
            }
            listed.append(ListedCredential(slot_name, ref))
        return listed

    def github_outputs(
        self, s: CredentialScope, credential: OidcTrust | IssuedSecret | NoCiCredential
    ) -> Sequence[GitHubOutput]:
        """Return the registry host and the robot's credentials (a pure function)."""
        if isinstance(credential, NoCiCredential):
            return []
        if isinstance(credential, OidcTrust):
            raise AdapterError(False, "Harbor does not support OIDC trust")
        repo, env = s.repository_id, s.environment
        host = urlsplit(self._base_url()).netloc
        return [
            GitHubOutput("variable", "HARBOR_REGISTRY", host, repo, env),
            GitHubOutput("secret", "HARBOR_USERNAME", credential.values["username"], repo, env),
            GitHubOutput("secret", "HARBOR_PASSWORD", credential.values["password"], repo, env),
        ]

    async def health_check(self) -> Health:
        """Read the current user (this verifies the credential); problems report `ok=False`."""
        try:
            async with self._client() as c:
                await c.call("GET", f"{API}/users/current")
        except AdapterError as exc:
            return Health(ok=False, detail=exc.message)
        return Health(ok=True)

    # ---- robots ----------------------------------------------------------
    @staticmethod
    def _current(prior: CredentialState) -> CredentialSlot:
        if prior.current is None:
            raise AdapterError(False, "no current slot")
        return prior.current

    @staticmethod
    def _robot_id(robot: JsonObj) -> str:
        robot_id = robot.get("id")
        if not isinstance(robot_id, int) or isinstance(robot_id, bool):
            raise AdapterError(False, "Harbor returned a robot account without an id")
        return str(robot_id)

    @staticmethod
    async def _robots(c: HarborClient, name_part: str) -> list[JsonObj]:
        """List robots whose name contains `name_part` (the listing spans the whole instance)."""
        listing = await c.call(
            "GET", f"{API}/robots", params={"q": f"name=~{name_part}", "page_size": PAGE_SIZE}
        )
        return json_list(listing)

    async def _find_robot(self, c: HarborClient, project: str, name: str) -> JsonObj | None:
        """Return the project robot `name`; its full name is `<prefix><project>+<name>`."""
        wanted = f"{project}+{name}"
        for robot in await self._robots(c, name):
            if str(robot.get("name", "")).endswith(wanted):
                self._robot_id(robot)
                return robot
        return None

    async def _create_robot(
        self,
        c: HarborClient,
        s: CredentialScope,
        base: CredentialState,
        slot: CredentialSlot,
        project: str,
    ) -> CredentialOutcome:
        body: JsonObj = {
            "name": slot.name,
            "description": "managed by trustbridge",
            "level": "project",
            "duration": ROBOT_DURATION_DAYS,
            "disable": False,
            "permissions": [
                {"kind": "project", "namespace": project, "access": robot_access(s.permission)}
            ],
        }
        post = await c.call("POST", f"{API}/robots", json=body, accept=CONFLICT)
        if post.status_code == HTTP_CONFLICT:
            raise AdapterError(True, "Harbor robot account was created concurrently")
        created = json_object(post)
        robot_id, name, secret = created.get("id"), created.get("name"), created.get("secret")
        if (
            not isinstance(robot_id, int)
            or isinstance(robot_id, bool)
            or not isinstance(name, str)
            or not isinstance(secret, str)
        ):
            raise AdapterError(False, "Harbor returned an unexpected robot response")
        expires_at = self._expiry(created) or self._clock() + timedelta(days=ROBOT_DURATION_DAYS)
        return self._outcome(
            base,
            slot,
            project=project,
            robot_id=str(robot_id),
            username=name,
            secret=secret,
            expires_at=expires_at,
            change=ChangeState.CREATED,
        )

    async def _refresh_robot(
        self, c: HarborClient, base: CredentialState, slot: CredentialSlot, robot: JsonObj
    ) -> CredentialOutcome:
        """Ask Harbor for a new generated secret; the old one stops working at once."""
        robot_id = self._robot_id(robot)
        patched = await c.call("PATCH", f"{API}/robots/{robot_id}", json={"secret": ""})
        secret = json_object(patched).get("secret")
        if not isinstance(secret, str):
            raise AdapterError(False, "Harbor returned an unexpected secret refresh response")
        project = str(robot["name"]).rsplit("+", 1)[0].rsplit("$", 1)[-1]
        expires_at = self._expiry(robot)
        return self._outcome(
            base,
            slot,
            project=project,
            robot_id=robot_id,
            username=str(robot["name"]),
            secret=secret,
            expires_at=expires_at,
            change=ChangeState.UPDATED,
        )

    @staticmethod
    def _expiry(robot: JsonObj) -> datetime | None:
        expires = robot.get("expires_at")
        if isinstance(expires, int) and not isinstance(expires, bool) and expires > 0:
            return datetime.fromtimestamp(expires, UTC)
        return None

    @staticmethod
    def _outcome(  # noqa: PLR0913 - one value per field of the persisted ref and the secret
        base: CredentialState,
        slot: CredentialSlot,
        *,
        project: str,
        robot_id: str,
        username: str,
        secret: str,
        expires_at: datetime | None,
        change: ChangeState,
    ) -> CredentialOutcome:
        issued = IssuedSecret(
            {"username": SecretStr(username), "password": SecretStr(secret)}, expires_at
        )
        ref = {"kind": "robot", "robot_id": robot_id, "project": project}
        state = CredentialState(
            current=replace(slot, ref=ref, delivered=False), previous=base.previous
        )
        return CredentialOutcome(Change(change), state, issued)

    async def _delete_slot(self, c: HarborClient, slot: CredentialSlot) -> bool:
        """Delete the robot a slot names (found by name in its project). True if it went."""
        project = slot.ref.get("project") or self._project_for_credentials()
        robot = await self._find_robot(c, project, slot.name)
        if robot is None:
            return False
        gone = await c.call("DELETE", f"{API}/robots/{self._robot_id(robot)}", accept=NOT_FOUND)
        return gone.status_code != HTTP_NOT_FOUND
