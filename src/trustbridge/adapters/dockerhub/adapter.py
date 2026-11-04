"""The Docker Hub adapter (NG5c, ADR 0029).

See `README.md` for the endpoints, which payload shapes Docker's documentation confirms, how the
per-repository grant maps to an organization access token, and what still needs a live check.
"""

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import ClassVar
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from trustbridge.adapters.dockerhub.client import DockerHubClient, JsonObj, json_object, seg
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
    tenant_prefix,
)

ORGS = "/v2/orgs"
NAMESPACES = "/v2/namespaces"

HTTP_NOT_FOUND = 404
HTTP_FORBIDDEN = 403
HTTP_CONFLICT = 409
NOT_FOUND = frozenset({HTTP_NOT_FOUND})
CONFLICT = frozenset({HTTP_CONFLICT})
FORBIDDEN = frozenset({HTTP_FORBIDDEN})

_log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://hub.docker.com"
MARKER = "managed by trustbridge"
"""The description of every repository and token the adapter creates: its ownership marker."""

TOKEN_TTL = timedelta(days=90)
CAPACITY_MESSAGE = (
    "Docker Hub refused to create the access token (HTTP 403): the organization is probably at "
    "its cap of access tokens (Team 10, Business 100); delete one in Docker Hub"
)
PAGE_SIZE = 100
MAX_PAGES = 50
CREDENTIAL_LIMIT = 10
"""Docker Team allows 10 organization access tokens (Business 100); expired ones count."""

# Repository scopes of an organization access token. Each scope also grants the lesser ones it
# includes (image-delete includes image-push, which includes image-pull), so one scope per
# permission is enough. `admin` adds repository edit but never repository admin (which deletes
# the repository) or the organization-level repository-create scope.
SCOPES: dict[Permission, tuple[str, ...]] = {
    "pull": ("scope-image-pull",),
    "push": ("scope-image-push",),
    "admin": ("scope-image-delete", "scope-repository-edit"),
}
ALL_REPOSITORIES = "*"
WEBHOOK_MANUAL_ACTION = (
    "Docker Hub documents no API to create webhooks: add the webhook in the Docker Hub "
    "repository's Webhooks tab"
)
REPOSITORY_MANUAL_ACTION = (
    "Docker Hub documents no API to delete a repository: delete it in Docker Hub if it is "
    "no longer needed"
)

_REPO_NAME = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
_REPO_NAME_MIN = 2
_REPO_NAME_MAX = 255
_TOKEN_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
"""Docker's access token ids are UUIDs (its examples: `a7a5ef25-8889-43a0-8cc7-f2a94268e861`)."""


def _format_time(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _normalised(resources: object) -> list[tuple[str, str, tuple[str, ...]]]:
    """Return resources in a comparable form (a token's resources, as sent or as read back)."""
    items = resources if isinstance(resources, list) else []
    return sorted(
        (
            str(item.get("type")),
            str(item.get("path")),
            tuple(sorted(str(scope) for scope in item.get("scopes") or [])),
        )
        for item in items
        if isinstance(item, dict)
    )


class DockerHubAdapter:
    """Provisions Docker Hub repositories and organization access tokens (OATs)."""

    name: ClassVar[str] = "dockerhub"
    api_version: ClassVar[int] = API_VERSION

    def __init__(
        self,
        config: AdapterConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Keep the per-tenant config; `transport` and `clock` exist for the contract tests.

        The namespace and `base_url` are checked here, so `github_outputs` (a pure function that
        reads the namespace) can never raise; the credential and the username are checked per call.
        """
        self._config = config
        self._namespace()
        self._base_url()
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(UTC))

    # ---- configuration ---------------------------------------------------
    def capabilities(self) -> Capabilities:
        """Describe Docker Hub: no OIDC, OATs only, two-phase rotation, unauthenticated hooks."""
        kinds: frozenset[HostKind] = frozenset({"github.com", "ghes", "ghe.com"})
        return Capabilities(
            oidc=False,
            oidc_hosts=frozenset(),
            oidc_requires_manual_step=False,
            ci_credential=True,
            rotation="two_phase",
            webhook=WebhookCapability(WebhookAuth.NONE, None, trigger_only=True),
            github_hosts=kinds,
            needs_privileged_credential=True,
            credential_limit=CREDENTIAL_LIMIT,
        )

    def _base_url(self) -> str:
        raw = self._config.connection.get("base_url")
        if raw is None:
            return DEFAULT_BASE_URL
        url = raw.get_secret_value().rstrip("/")
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.netloc
            or "@" in parts.netloc
            or parts.query
            or parts.fragment
        ):
            msg = "Docker Hub base_url must be an https URL without credentials, query or fragment"
            raise AdapterConfigurationError(msg)
        return url

    def _namespace(self) -> str:
        raw = self._config.connection.get("namespace")
        if raw is None or not raw.get_secret_value():
            raise AdapterConfigurationError("Docker Hub connection data has no namespace")
        return raw.get_secret_value()

    def _client(self) -> DockerHubClient:
        pat = self._config.privileged_credential
        if pat is None:
            raise AdapterConfigurationError("Docker Hub organization-owner PAT is not configured")
        user = self._config.connection.get("username")
        if user is None or not user.get_secret_value():
            raise AdapterConfigurationError("Docker Hub connection data has no username")
        return DockerHubClient(self._base_url(), user.get_secret_value(), pat, self._transport)

    @staticmethod
    def _repo_name(key: str) -> str:
        if not _REPO_NAME.fullmatch(key) or not _REPO_NAME_MIN <= len(key) <= _REPO_NAME_MAX:
            msg = "repository key must be 2-255 lowercase letters, digits and '.', '_', '-'"
            raise AdapterConfigurationError(msg)
        return key

    def _resources(self, s: CredentialScope) -> list[JsonObj]:
        """Return the token's resource grant: one repository, or every repository of the org.

        `*` is the glob Docker documents for all repositories in an organization.
        """
        key = s.platform_repository_key
        target = ALL_REPOSITORIES if key is None else self._repo_name(key)
        path = f"{self._namespace()}/{target}"
        return [{"type": "TYPE_REPO", "path": path, "scopes": list(SCOPES[s.permission])}]

    # ---- projects (the Docker Hub namespace) -----------------------------
    async def ensure_project(
        self, t: Tenant, s: ProjectSpec, prior: ResourceState | None
    ) -> Applied:
        """Check that the tenant's organization exists; Docker Hub has no projects to create.

        The organization settings endpoint answers 404 for an unknown organization, so an empty
        one (200) is told apart from a missing one; 403 and any other status are errors.
        """
        del t, s, prior
        namespace = self._namespace()
        async with self._client() as c:
            found = await c.call("GET", f"{ORGS}/{seg(namespace)}/settings", accept=NOT_FOUND)
        if found.status_code == HTTP_NOT_FOUND:
            raise AdapterError(False, "Docker Hub organization was not found")
        ref = {"kind": "project", "namespace": namespace}
        return Applied(Change(ChangeState.UNCHANGED), ResourceState(name=namespace, ref=ref))

    @staticmethod
    def _repos_path(namespace: str, repository: str | None = None) -> str:
        base = f"{NAMESPACES}/{seg(namespace)}/repositories"
        return base if repository is None else f"{base}/{seg(repository)}"

    # ---- repositories ----------------------------------------------------
    async def ensure_repository(
        self, p: ProjectRef, s: RepoSpec, prior: ResourceState | None
    ) -> Applied:
        """Create a private repository; never adopt an unmarked one or flip a public one private."""
        del prior
        if s.package_type.lower() != "docker":
            raise AdapterConfigurationError("Docker Hub repositories hold docker images only")
        namespace = p.project.ref.get("namespace", p.project.name)
        name = self._repo_name(s.key)
        path = self._repos_path(namespace, name)
        body: JsonObj = {
            "name": name,
            "namespace": namespace,
            "description": MARKER,
            "is_private": True,
        }
        async with self._client() as c:
            found = await c.call("GET", path, accept=NOT_FOUND)
            state = ChangeState.UNCHANGED
            if found.status_code == HTTP_NOT_FOUND:
                post = await c.call("POST", self._repos_path(namespace), json=body, accept=CONFLICT)
                if post.status_code == HTTP_CONFLICT:
                    found = await c.call("GET", path)
                else:
                    state = ChangeState.CREATED
            if state is ChangeState.UNCHANGED:
                existing = json_object(found)
                if existing.get("description") != MARKER:
                    raise AdapterError(False, "repository exists and is not managed by trustbridge")
                if existing.get("is_private") is not True:
                    msg = (
                        f"repository {namespace}/{name} is public: trustbridge never changes "
                        "visibility, set it to private in the repository's Settings in Docker Hub"
                    )
                    raise AdapterError(False, msg)
        ref = {"kind": "repository", "namespace": namespace, "repository": name}
        return Applied(Change(state), ResourceState(name=name, ref=ref))

    # ---- webhooks and deleting resources ---------------------------------
    async def ensure_webhook(self, t: WebhookTarget, prior: ResourceState | None) -> Applied:
        """Report a manual step: no Docker Hub webhook API is documented (see the README)."""
        del t, prior
        change = Change(ChangeState.NEEDS_MANUAL_ACTION, manual_action=WEBHOOK_MANUAL_ACTION)
        return Applied(change, None)

    async def delete(self, prior: ResourceState) -> Applied:
        """Never delete a namespace or a repository: report what is left for a person to do."""
        kind = prior.ref.get("kind")
        if kind == "repository":
            change = Change(ChangeState.NEEDS_MANUAL_ACTION, manual_action=REPOSITORY_MANUAL_ACTION)
        elif kind in {"project", "webhook"}:
            change = Change(ChangeState.UNCHANGED)
        else:
            raise AdapterError(False, "unknown resource kind in state")
        return Applied(change, None)

    # ---- credentials -----------------------------------------------------
    async def ensure_credential(
        self, s: CredentialScope, prior: CredentialState
    ) -> CredentialOutcome:
        """Issue an OAT, or confirm (and re-grant, on drift) the delivered one."""
        slot = self._current(prior)
        namespace = self._namespace()
        async with self._client() as c:
            existing = await self._find_tokens(c, namespace, slot.name)
            # Only the token this slot recorded counts. If it is gone or unusable the secret
            # cannot be read back, so `_issue` returns a fresh one with `delivered=False` and the
            # core delivers it again (ADR 0029, "Secret issuance").
            recorded = slot.ref.get("token_id")
            current = next((t for t in existing if t["id"] == recorded and self._usable(t)), None)
            if slot.delivered and current is not None:
                confirmed = await self._confirm(c, s, prior, current, existing)
                if confirmed is not None:
                    return confirmed
            return await self._issue(c, s, prior, namespace, existing)

    async def rotate_begin(self, s: CredentialScope, prior: CredentialState) -> CredentialOutcome:
        """Create the next generation's OAT; `previous` stays valid (two-phase rotation)."""
        slot = self._current(prior)
        namespace = self._namespace()
        async with self._client() as c:
            existing = await self._find_tokens(c, namespace, slot.name)
            return await self._issue(c, s, prior, namespace, existing)

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
        """Delete the OATs of `current` and `previous`; absent is UNCHANGED."""
        del s
        removed = False
        async with self._client() as c:
            for slot in (prior.current, prior.previous):
                if slot is not None and await self._delete_slot(c, slot):
                    removed = True
        change = Change(ChangeState.DELETED if removed else ChangeState.UNCHANGED)
        return CredentialOutcome(change, CredentialState(current=None), None)

    async def list_credentials(self, t: Tenant) -> Sequence[ListedCredential]:
        """List the tenant's trustbridge-named OATs (label prefix `tb-<tenant hex>-`)."""
        namespace = self._namespace()
        async with self._client() as c:
            tokens = await self._find_tokens(c, namespace, tenant_prefix(t.id), prefix=True)
        return [
            ListedCredential(str(tok["label"]), {"kind": "oat", "token_id": tok["id"]})
            for tok in tokens
        ]

    def github_outputs(
        self, s: CredentialScope, credential: OidcTrust | IssuedSecret | NoCiCredential
    ) -> Sequence[GitHubOutput]:
        """Return the Docker Hub login (organization name and OAT); a pure function."""
        if not isinstance(credential, IssuedSecret):
            return []
        repo, env = s.repository_id, s.environment
        return [
            GitHubOutput("variable", "DOCKERHUB_USERNAME", self._namespace(), repo, env),
            GitHubOutput("secret", "DOCKERHUB_TOKEN", credential.values["token"], repo, env),
        ]

    async def health_check(self) -> Health:
        """Log in and list one repository; configuration and transport problems are `ok=False`."""
        try:
            path = self._repos_path(self._namespace())
            async with self._client() as c:
                await c.call("GET", path, params={"page_size": "1"})
        except AdapterError as exc:
            return Health(ok=False, detail=exc.message)
        return Health(ok=True)

    # ---- tokens ----------------------------------------------------------
    def _current(self, prior: CredentialState) -> CredentialSlot:
        if prior.current is None:
            raise AdapterError(False, "no current slot")
        self._check_owned(prior.current.name)
        return prior.current

    def _check_owned(self, name: str) -> None:
        """Only tokens named with this tenant's prefix are ever touched or deleted."""
        if not name.startswith(tenant_prefix(self._config.tenant.id)):
            raise AdapterError(False, "credential name is not a trustbridge name of this tenant")

    def _usable(self, token: JsonObj) -> bool:
        if token.get("is_active", True) is not True:
            return False
        expires = _parse_time(token.get("expires_at"))
        return expires is None or expires > self._clock()

    @staticmethod
    async def _find_tokens(
        c: DockerHubClient, namespace: str, label: str, *, prefix: bool = False
    ) -> list[JsonObj]:
        path = f"{ORGS}/{seg(namespace)}/access-tokens"
        found: list[JsonObj] = []
        for page in range(1, MAX_PAGES + 1):
            params = {"page": str(page), "page_size": str(PAGE_SIZE)}
            body = json_object(await c.call("GET", path, params=params))
            results = body.get("results")
            if not isinstance(results, list):
                raise AdapterError(False, "Docker Hub returned a token listing that is not a list")
            for token in results:
                # The listing spans the whole organization: only an entry this call selects
                # must be sound.
                name = str(token.get("label") or "") if isinstance(token, dict) else ""
                if name.startswith(label) if prefix else name == label:
                    token_id = token.get("id")
                    if not isinstance(token_id, str) or not _TOKEN_ID.fullmatch(token_id):
                        msg = "Docker Hub returned a token entry without a valid id"
                        raise AdapterError(False, msg)
                    found.append(token)
            if not body.get("next"):
                return found
        raise AdapterError(False, "Docker Hub token listing has too many pages")

    async def _confirm(
        self,
        c: DockerHubClient,
        s: CredentialScope,
        prior: CredentialState,
        token: JsonObj,
        existing: list[JsonObj],
    ) -> CredentialOutcome | None:
        """Keep a delivered, usable OAT: PATCH a drifted grant, delete other same-label tokens.

        Returns None when the token is gone (404 on the read): the caller issues a new one. The
        other same-label tokens are leftovers whose secret is lost (a cleanup that failed after
        an issue); the recorded token is never among them.
        """
        namespace = self._namespace()
        path = f"{ORGS}/{seg(namespace)}/access-tokens/{seg(token['id'])}"
        desired = self._resources(s)
        read = await c.call("GET", path, accept=NOT_FOUND)
        if read.status_code == HTTP_NOT_FOUND:
            return None
        state = ChangeState.UNCHANGED
        if _normalised(json_object(read).get("resources")) != _normalised(desired):
            await c.call("PATCH", path, json={"resources": desired})
            state = ChangeState.UPDATED
        for stale in existing:
            if stale["id"] != token["id"] and await self._delete_token(c, namespace, stale["id"]):
                state = ChangeState.UPDATED
        return CredentialOutcome(Change(state), prior, None)

    async def _create_token(
        self,
        c: DockerHubClient,
        namespace: str,
        body: JsonObj,
        existing: list[JsonObj],
        live: str | None,
    ) -> tuple[JsonObj, list[JsonObj]]:
        """POST the token; on a 403 (the token limit) free stale same-label tokens and retry once.

        Returns the response and the same-label tokens still to delete. Only tokens other than
        the recorded live one are deleted before the create; the live one waits for its
        replacement. Still refused with nothing stale to free is a non-retryable capacity error.
        """
        path = f"{ORGS}/{seg(namespace)}/access-tokens"
        response = await c.call("POST", path, json=body, accept=FORBIDDEN)
        remaining = existing
        if response.status_code == HTTP_FORBIDDEN:
            stale = [t for t in existing if t["id"] != live]
            if not stale:
                raise AdapterError(False, CAPACITY_MESSAGE)
            for old in stale:
                await self._delete_token(c, namespace, old["id"])
            remaining = [t for t in existing if t["id"] == live]
            response = await c.call("POST", path, json=body, accept=FORBIDDEN)
            if response.status_code == HTTP_FORBIDDEN:
                raise AdapterError(False, CAPACITY_MESSAGE)
        return json_object(response), remaining

    async def _issue(
        self,
        c: DockerHubClient,
        s: CredentialScope,
        base: CredentialState,
        namespace: str,
        existing: list[JsonObj],
    ) -> CredentialOutcome:
        """Create the OAT (secret shown once), then delete the older same-label ones.

        Two phases: a failed create (5xx, 429) leaves every old token in place and is retried as
        the error says; a 403 first frees stale tokens (see `_create_token`). Older same-label
        tokens (their secret is lost, and they count toward the limit) go only after the create
        succeeded, and never the new one: the list was read before the create. A delete that
        fails then is logged and tolerated, because the secret must not be lost: the next
        reconcile (`_confirm`) deletes every same-label token but the recorded one.
        """
        slot = self._current(base)
        expires = self._clock() + TOKEN_TTL
        body: JsonObj = {
            "label": slot.name,
            "description": MARKER,
            "resources": self._resources(s),
            "expires_at": _format_time(expires),
        }
        created, remaining = await self._create_token(
            c, namespace, body, existing, slot.ref.get("token_id")
        )
        secret = created.get("token")
        token_id = created.get("id")
        if not isinstance(secret, str) or not isinstance(token_id, str):
            raise AdapterError(False, "Docker Hub returned an unexpected token response")
        leftover = 0
        for old in remaining:
            try:
                await self._delete_token(c, namespace, old["id"])
            except AdapterError as exc:
                leftover += 1
                _log.warning("dockerhub: stale token cleanup failed: %s", exc.message)
        issued = IssuedSecret({"token": SecretStr(secret)}, expires)
        ref = {"kind": "oat", "token_id": token_id}
        state = CredentialState(
            current=replace(slot, ref=ref, delivered=False), previous=base.previous
        )
        detail = (
            f"{leftover} stale token(s) remain; the next reconcile deletes them"
            if leftover
            else None
        )
        change = Change(ChangeState.UPDATED if existing else ChangeState.CREATED, detail=detail)
        return CredentialOutcome(change, state, issued)

    @staticmethod
    async def _delete_token(c: DockerHubClient, namespace: str, token_id: str) -> bool:
        """Delete one OAT; True only when Docker confirmed it (2xx), False when it was gone."""
        if not _TOKEN_ID.fullmatch(token_id):
            raise AdapterError(False, "Docker Hub token id is not a UUID; refusing to delete it")
        path = f"{ORGS}/{seg(namespace)}/access-tokens/{seg(token_id)}"
        return (await c.call("DELETE", path, accept=NOT_FOUND)).is_success

    async def _delete_slot(self, c: DockerHubClient, slot: CredentialSlot) -> bool:
        """Delete the OATs a slot's label names. True if any went."""
        self._check_owned(slot.name)
        namespace = self._namespace()
        removed = False
        for token in await self._find_tokens(c, namespace, slot.name):
            removed |= await self._delete_token(c, namespace, token["id"])
        return removed
