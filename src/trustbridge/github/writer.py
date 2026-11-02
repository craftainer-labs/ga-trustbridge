"""Writes adapter outputs to GitHub: encrypted secrets, variables and environments.

The core alone writes these (adapters never call GitHub). Secrets are sealed with libsodium to
the target's public key, so the plaintext only exists in this process. Every write is an
idempotent create-or-update, and every call uses a token down-scoped to the one repository and
the one permission it needs.
"""

from collections.abc import Sequence

from pydantic import SecretStr

from trustbridge.adapters.interface import GitHubOutput, Tenant
from trustbridge.adapters.store import DeliveredOutput, OutputError
from trustbridge.github.availability import HostNotReadyError
from trustbridge.github.client import HTTP_NOT_FOUND, GitHubApiError
from trustbridge.github.gateway import Call, TenantGateway, enc
from trustbridge.github.sealed import seal

_ORG_VISIBILITY = "private"
"""Org-wide outputs are visible to private and internal repositories, never to public ones."""


class GitHubOutputWriter:
    """`OutputWriter` implemented over GitHub's REST API."""

    def __init__(self, gateway: TenantGateway) -> None:
        """Use `gateway` for tokens, requests and repository-name lookups."""
        self._gw = gateway

    async def apply(self, tenant: Tenant, outputs: Sequence[GitHubOutput]) -> None:
        """Create or update each output; raises `OutputError`."""
        try:
            for out in outputs:
                if out.kind == "secret":
                    await self._put_secret(tenant, out)
                elif out.kind == "variable":
                    await self._put_variable(tenant, out)
                else:
                    await self._ensure_environment(tenant, out)
        except GitHubApiError as exc:
            raise OutputError(
                f"GitHub write failed: {exc.message} (HTTP {exc.status})", retryable=exc.retryable
            ) from exc
        except HostNotReadyError as exc:
            raise OutputError(str(exc), retryable=True) from exc

    async def remove(self, tenant: Tenant, outputs: Sequence[DeliveredOutput]) -> None:
        """Delete secrets and variables; absent is fine.

        An `environment` output is never deleted: the environment may hold other secrets, and
        removing it would also drop its protection rules.
        """
        try:
            for out in outputs:
                if out.kind == "environment":
                    continue
                await self._delete(tenant, out)
        except GitHubApiError as exc:
            raise OutputError(
                f"GitHub delete failed: {exc.message} (HTTP {exc.status})", retryable=exc.retryable
            ) from exc
        except HostNotReadyError as exc:
            raise OutputError(str(exc), retryable=True) from exc

    # ---- paths ----------------------------------------------------------
    async def _base(
        self, tenant: Tenant, repository_id: int | None, environment: str | None
    ) -> str:
        """Return the `.../actions` or `.../environments/<env>` base path of an output."""
        if repository_id is None:
            if environment is not None:
                msg = "an environment output needs a repository"
                raise OutputError(msg, retryable=False)
            return f"/orgs/{enc(tenant.org)}/actions"
        if environment is not None:
            return f"/repositories/{repository_id}/environments/{enc(environment)}"
        return f"/repos/{await self._gw.full_name(tenant, repository_id)}/actions"

    @staticmethod
    def _scope(
        repository_id: int | None, permission: str
    ) -> tuple[list[int] | None, dict[str, str]]:
        if repository_id is None:
            return None, {f"organization_{permission}": "write"}
        return [repository_id], {permission: "write"}

    # ---- secrets and variables -----------------------------------------
    async def _put_secret(self, tenant: Tenant, out: GitHubOutput) -> None:
        base = await self._base(tenant, out.repository_id, out.environment)
        ids, perms = self._scope(out.repository_id, "secrets")
        key = await self._gw.call(tenant, Call("GET", f"{base}/secrets/public-key", perms, ids))
        plain = out.value.get_secret_value() if isinstance(out.value, SecretStr) else out.value
        body: dict[str, object] = {
            "encrypted_value": seal(str(key.data["key"]), plain),
            "key_id": str(key.data["key_id"]),
        }
        if out.repository_id is None:
            body["visibility"] = _ORG_VISIBILITY
        await self._gw.call(
            tenant, Call("PUT", f"{base}/secrets/{enc(out.name)}", perms, ids, body)
        )

    async def _put_variable(self, tenant: Tenant, out: GitHubOutput) -> None:
        base = await self._base(tenant, out.repository_id, out.environment)
        ids, perms = self._scope(out.repository_id, "variables")
        value = out.value.get_secret_value() if isinstance(out.value, SecretStr) else out.value
        try:
            await self._gw.call(
                tenant,
                Call("PATCH", f"{base}/variables/{enc(out.name)}", perms, ids, {"value": value}),
            )
        except GitHubApiError as exc:
            if exc.status != HTTP_NOT_FOUND:
                raise
            body: dict[str, object] = {"name": out.name, "value": value}
            if out.repository_id is None:
                body["visibility"] = _ORG_VISIBILITY
            await self._gw.call(tenant, Call("POST", f"{base}/variables", perms, ids, body))

    async def _ensure_environment(self, tenant: Tenant, out: GitHubOutput) -> None:
        if out.repository_id is None:
            msg = "an environment output needs a repository"
            raise OutputError(msg, retryable=False)
        full = await self._gw.full_name(tenant, out.repository_id)
        await self._gw.call(
            tenant,
            Call(
                "PUT",
                f"/repos/{full}/environments/{enc(out.name)}",
                {"administration": "write"},
                [out.repository_id],
                {},
            ),
        )

    async def _delete(self, tenant: Tenant, out: DeliveredOutput) -> None:
        base = await self._base(tenant, out.repository_id, out.environment)
        noun = "secrets" if out.kind == "secret" else "variables"
        ids, perms = self._scope(out.repository_id, noun)
        try:
            await self._gw.call(
                tenant, Call("DELETE", f"{base}/{noun}/{enc(out.name)}", perms, ids)
            )
        except GitHubApiError as exc:
            if exc.status != HTTP_NOT_FOUND:
                raise


class GitHubEnvironmentInspector:
    """`EnvironmentInspector` that reads an environment's protection rules from GitHub.

    "Protected" (ADR 0029 item 12) means the environment exists and has at least one entry in
    `protection_rules` (required reviewers, a wait timer or a deployment branch policy). An
    environment that does not exist is not protected.
    """

    def __init__(self, gateway: TenantGateway) -> None:
        """Use `gateway` for tokens and requests."""
        self._gw = gateway

    async def is_protected(self, tenant: Tenant, repository_id: int, environment: str) -> bool:
        """Return True when the environment has protection rules."""
        full = await self._gw.full_name(tenant, repository_id)
        try:
            resp = await self._gw.call(
                tenant,
                Call(
                    "GET",
                    f"/repos/{full}/environments/{enc(environment)}",
                    {"administration": "read"},
                    [repository_id],
                ),
            )
        except GitHubApiError as exc:
            if exc.status == HTTP_NOT_FOUND:
                return False
            raise
        return bool(resp.data.get("protection_rules"))
