"""GitHub host configuration (ADR 0030): the `GITHUB_HOSTS` schema, its loader and settings checks.

`validate_hosts` is the fatal tier of the startup checks: it needs nothing but the settings, and
a failure stops the process. Checks that need the signer, the network or GitHub live in
`availability.py` and never stop a process.
"""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from trustbridge.adapters.interface import GitHubHost, HostKind
from trustbridge.signer.config import FileSignerConfig, SignerConfig

DEFAULT_HOST_RECHECK_SECONDS = 60
"""`HOST_RECHECK_SECONDS` default: how often non-ready hosts are re-checked."""

GITHUB_COM_ISSUER = "https://token.actions.githubusercontent.com"

_OIDC_ID_CLAIMS_DEFAULT: dict[str, bool] = {"github.com": True, "ghes": False, "ghe.com": False}
_GHES_API_PATH = "/api/v3"
_GHE_COM_SUFFIX = ".ghe.com"


class HostSettingsError(Exception):
    """Invalid host or signer settings; a process raises this at startup and exits non-zero."""

    def __init__(self, problems: Sequence[str]) -> None:
        """Keep every problem found, not only the first."""
        super().__init__("; ".join(problems))
        self.problems = list(problems)


class GitHubHostConfig(BaseModel):
    """One entry of `GITHUB_HOSTS`; `app_id` and `webhook_secret_ref` are absent if unregistered."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: HostKind
    web_url: str
    api_url: str
    oidc_issuer: str
    registry_host: str
    oidc_id_claims: bool
    signer: str
    app_id: int | None = None
    webhook_secret_ref: str | None = None
    ca_bundle: Path | None = None

    @model_validator(mode="after")
    def _registration_pair(self) -> Self:
        if (self.app_id is None) != (self.webhook_secret_ref is None):
            msg = f"host {self.name!r}: app_id and webhook_secret_ref must be set together"
            raise ValueError(msg)
        return self

    @property
    def registered(self) -> bool:
        """True once the host has an App (id and webhook secret reference)."""
        return self.app_id is not None

    def to_host(self) -> GitHubHost:
        """Project to the adapter-facing `GitHubHost` (ADR 0029)."""
        return GitHubHost(
            name=self.name,
            kind=self.kind,
            api_url=self.api_url,
            oidc_issuer=self.oidc_issuer,
            oidc_id_claims=self.oidc_id_claims,
            registry_host=self.registry_host,
        )

    def with_registration(self, app_id: int, webhook_secret_ref: str) -> Self:
        """Return the config with a registration overlaid (the entry itself is read-only)."""
        return self.model_copy(update={"app_id": app_id, "webhook_secret_ref": webhook_secret_ref})


def _fields(exc: ValidationError) -> str:
    return ", ".join(".".join(str(p) for p in e["loc"]) or e["msg"] for e in exc.errors())


def load_hosts(raw: str | Sequence[Mapping[str, Any]]) -> list[GitHubHostConfig]:
    """Load `GITHUB_HOSTS` (JSON text or a decoded list), applying the kind-dependent default.

    `oidc_id_claims` defaults to True for `github.com` and False for `ghes` and `ghe.com` when an
    entry omits it; an explicit value always wins. An unknown `kind` is a validation error.
    """
    entries = json.loads(raw) if isinstance(raw, str) else list(raw)
    hosts: list[GitHubHostConfig] = []
    for entry in entries:
        data = dict(entry)
        if "oidc_id_claims" not in data:
            kind = data.get("kind")
            if kind not in _OIDC_ID_CLAIMS_DEFAULT:
                msg = f"host {data.get('name')!r}: unknown kind {kind!r}"
                raise HostSettingsError([msg])
            data["oidc_id_claims"] = _OIDC_ID_CLAIMS_DEFAULT[str(kind)]
        try:
            hosts.append(GitHubHostConfig.model_validate(data))
        except ValidationError as exc:
            raise HostSettingsError(
                [f"host {data.get('name')!r}: {exc.error_count()} invalid field(s): {_fields(exc)}"]
            ) from exc
    return hosts


def _check_url_shape(host: GitHubHostConfig, *, dev_mode: bool) -> list[str]:
    problems: list[str] = []
    web = urlparse(host.web_url)
    api = urlparse(host.api_url)
    issuer = urlparse(host.oidc_issuer)
    for label, parsed in (("web_url", web), ("api_url", api), ("oidc_issuer", issuer)):
        if parsed.scheme != "https" and not (dev_mode and parsed.scheme == "http"):
            problems.append(f"host {host.name!r}: {label} must be an https URL")
        if not parsed.hostname:
            problems.append(f"host {host.name!r}: {label} has no host")
    if host.kind == "github.com":
        if (api.hostname, api.path.rstrip("/")) != ("api.github.com", ""):
            problems.append(
                f"host {host.name!r}: github.com api_url must be https://api.github.com"
            )
    elif host.kind == "ghes":
        if api.hostname != web.hostname or api.path.rstrip("/") != _GHES_API_PATH:
            problems.append(
                f"host {host.name!r}: ghes api_url must be https://HOST{_GHES_API_PATH}"
            )
    else:
        sub = (web.hostname or "").removesuffix(_GHE_COM_SUFFIX)
        if (
            not (web.hostname or "").endswith(_GHE_COM_SUFFIX)
            or api.hostname != f"api.{sub}{_GHE_COM_SUFFIX}"
        ):
            problems.append(f"host {host.name!r}: ghe.com api_url must be https://api.SUB.ghe.com")
        elif api.path.rstrip("/") != "":
            problems.append(f"host {host.name!r}: ghe.com api_url must have no path")
    return problems


def validate_hosts(
    hosts: Sequence[GitHubHostConfig],
    signers: Mapping[str, SignerConfig],
    *,
    production: bool,
    dev_mode: bool = False,
) -> list[str]:
    """Run the settings-only startup checks (ADR 0030, invariant 3).

    Raises `HostSettingsError` listing every problem: duplicate host names, non-https URLs
    outside the flagged dev mode, a wrong `api_url` shape for the kind, a `signer` that names no
    `SIGNERS` entry, a `file` signer in production. (`app_id` without `webhook_secret_ref`, or
    the reverse, is rejected when the entry is parsed.) Returns warnings, for example a signer
    entry no host uses.
    """
    problems: list[str] = []
    seen: set[str] = set()
    for host in hosts:
        if host.name in seen:
            problems.append(f"duplicate host name {host.name!r}")
        seen.add(host.name)
        problems.extend(_check_url_shape(host, dev_mode=dev_mode))
        entry = signers.get(host.signer)
        if entry is None:
            problems.append(f"host {host.name!r}: signer {host.signer!r} is not in SIGNERS")
        elif production and isinstance(entry, FileSignerConfig):
            problems.append(f"host {host.name!r}: the file signer is refused in production")
    if problems:
        raise HostSettingsError(problems)
    used = {h.signer for h in hosts}
    return [f"signer {name!r} is not used by any host" for name in signers if name not in used]
