"""trustbridge settings: `TrustbridgeSettings` extends the template's `app.config.Settings`.

ADR 0028 "Settings": `extra="forbid"` is set explicitly, there is no `env_file` and no
secrets dir (the environment source silently ignores unrelated names, so a misspelt variable
is caught separately by `check_environment`), settings whose absence is dangerous
(`GITHUB_HOSTS`, `SIGNERS`) have no default, and the effective defaults are logged at startup
(`log_effective_settings`).

Interface for NG4 and later items: import `TrustbridgeSettings`,
`get_trustbridge_settings()`, `GitHubHostConfig` and `SignerConfig` from here. The host and
signer models hold only what settings validation needs; NG4 owns the behaviour behind them.
"""

import difflib
import ipaddress
import json
import logging
import os
import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.functional_validators import BeforeValidator
from pydantic_settings import NoDecode, SettingsConfigDict

from app.config import Settings

logger = logging.getLogger(__name__)

HostKind = Literal["github.com", "ghes", "ghe.com"]

# GitHub documents a 25 MB payload cap (ADR 0028 open issue: not re-fetched in this pass).
DEFAULT_WEBHOOK_MAX_BODY_BYTES = 25 * 1024 * 1024
_HOST_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{0,62}$")
_GHES_API_PATH = "/api/v3"


class SettingsError(ValueError):
    """The process environment holds a setting the process must refuse to start with."""


class VaultAuth(BaseModel):
    """How a `vault-transit` signer authenticates; credentials are file references only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    method: Literal["kubernetes", "approle"]
    role: str
    secret_id_file: Path | None = None
    jwt_file: Path | None = None


class FileSignerConfig(BaseModel):
    """A PEM on disk (dev only; refused in production)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["file"]
    path: Path


class VaultTransitSignerConfig(BaseModel):
    """A non-exportable Vault Transit RSA key."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["vault-transit"]
    addr: str
    mount: str = "transit"
    key: str
    auth: VaultAuth


SignerConfig = Annotated[FileSignerConfig | VaultTransitSignerConfig, Field(discriminator="kind")]


class GitHubHostConfig(BaseModel):
    """One configured GitHub host (ADR 0030 "Hosts").

    `app_id` and `webhook_secret_ref` are both set or both absent; absent means the host is
    `unregistered`. `oidc_id_claims` has no field default: the loader below applies the
    kind-dependent default (True for github.com, False for ghes and ghe.com) when an entry
    omits it, and an explicit value always wins.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: HostKind
    web_url: str
    api_url: str
    oidc_issuer: str
    registry_host: str
    oidc_id_claims: bool
    app_id: int | None = None
    signer: str
    webhook_secret_ref: str | None = None
    ca_bundle: Path | None = None

    @model_validator(mode="before")
    @classmethod
    def _apply_oidc_id_claims_default(cls, data: Any) -> Any:  # noqa: ANN401 -- pydantic hook
        """Default `oidc_id_claims` by `kind` when the entry omits it."""
        if isinstance(data, dict) and "oidc_id_claims" not in data:
            return {**data, "oidc_id_claims": data.get("kind") == "github.com"}
        return data

    @property
    def registered(self) -> bool:
        """Whether the host has an App (both `app_id` and `webhook_secret_ref` set)."""
        return self.app_id is not None and self.webhook_secret_ref is not None

    def problems(self, *, allow_http: bool) -> list[str]:
        """Settings-only problems with this entry (every one is fatal at startup)."""
        found: list[str] = []
        if not _HOST_NAME.match(self.name):
            found.append(f"host name {self.name!r} must match {_HOST_NAME.pattern}")
        for label, url in (("web_url", self.web_url), ("api_url", self.api_url)):
            scheme = urlsplit(url).scheme
            if scheme != "https" and not (allow_http and scheme == "http"):
                found.append(f"host {self.name}: {label} must be https")
        if (self.app_id is None) != (self.webhook_secret_ref is None):
            found.append(
                f"host {self.name}: app_id and webhook_secret_ref must be both set or both absent"
            )
        found.extend(self._api_url_shape())
        return found

    def _api_url_shape(self) -> list[str]:
        """The `api_url` shape for this host kind (ADR 0030 table)."""
        parts = urlsplit(self.api_url)
        host = parts.hostname or ""
        path = parts.path.rstrip("/")
        ok = {
            "github.com": host == "api.github.com" and path == "",
            "ghes": bool(host) and not host.startswith("api.") and path == _GHES_API_PATH,
            "ghe.com": host.startswith("api.") and host.endswith(".ghe.com") and path == "",
        }[self.kind]
        return [] if ok else [f"host {self.name}: api_url has the wrong shape for kind {self.kind}"]


def _split_proxies(value: Any) -> Any:  # noqa: ANN401 -- pydantic hook
    """Accept `TRUSTED_PROXIES` as a JSON list or a comma-separated string; `*` stays `*`."""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            return json.loads(text)
        return [item.strip() for item in text.split(",") if item.strip()]
    return value


TrustedProxies = Annotated[list[str] | None, NoDecode, BeforeValidator(_split_proxies)]


class TrustbridgeSettings(Settings):
    """The template's settings plus hosts, signers, queue, retention, limiter and tuning.

    Defaults for the retention and tuning fields are ADR 0028 decision 9. NG3 chose
    `max_delivery_attempts` and the backoff bounds (documented in
    `src/trustbridge/worker/README.md`).
    """

    model_config = SettingsConfigDict(extra="forbid")  # no env_file, no secrets_dir

    # No defaults: absence is dangerous (ADR 0028 "Settings").
    github_hosts: list[GitHubHostConfig]
    signers: dict[str, SignerConfig]
    # Flagged dev mode: allows http:// host URLs. Refused in production.
    allow_insecure_github_urls: bool = False

    # Ingress.
    webhook_max_body_bytes: int = Field(default=DEFAULT_WEBHOOK_MAX_BODY_BYTES, gt=0)
    webhook_secret_read_timeout_seconds: float = Field(default=5.0, gt=0)
    webhook_secret_refresh_min_seconds: float = Field(default=60.0, ge=0)

    # Limiter (ADR 0028 "Abuse limits on the webhook route").
    webhook_rate_limit_enabled: bool = True
    webhook_rate_limit_requests: int = Field(default=300, gt=0)
    webhook_rate_limit_window_seconds: int = Field(default=60, gt=0)
    webhook_rate_limit_timeout_ms: int = Field(default=50, gt=0)
    webhook_rate_limit_exempt_cidrs: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = Field(
        default_factory=list
    )
    # None = unset: refused in production. [] = no proxy, use the socket peer.
    trusted_proxies: TrustedProxies = None

    # Queue and processing.
    max_delivery_attempts: int = Field(default=8, gt=0)
    retry_backoff_base_seconds: float = Field(default=30.0, gt=0)
    retry_backoff_max_seconds: float = Field(default=1800.0, gt=0)
    busy_retry_seconds: float = Field(default=10.0, gt=0)
    worker_block_ms: int = Field(default=5000, gt=0)
    worker_batch_size: int = Field(default=10, gt=0)
    # Pending entries idle at least this long are claimed from a dead consumer.
    stream_claim_idle_seconds: float = Field(default=120.0, gt=0)
    stream_trim_interval_seconds: float = Field(default=300.0, gt=0)
    relay_interval_seconds: float = Field(default=5.0, gt=0)
    # At most this many ids are relayed per call (tick, sweep or host-ready).
    relay_batch_size: int = Field(default=5000, gt=0)
    # The full re-relay sweep (ADR 0028 decision 9: relay recheck 15 min).
    relay_recheck_seconds: float = Field(default=900.0, gt=0)
    relay_resweep_min_age_seconds: float = Field(default=60.0, ge=0)
    delivery_reconcile_interval_seconds: float = Field(default=300.0, gt=0)
    delivery_reconcile_lookback_hours: int = Field(default=72, gt=0)
    delivery_reconcile_max_per_run: int = Field(default=100, gt=0)
    retention_interval_seconds: float = Field(default=3600.0, gt=0)

    # Retention (ADR 0028 decision 9).
    body_retention_processed_days: int = Field(default=7, gt=0)
    body_retention_dead_days: int = Field(default=30, gt=0)
    delivery_id_retention_days: int = Field(default=30, gt=0)
    dead_stream_retention_days: int = Field(default=30, gt=0)

    # Leases and adapter calls.
    lease_ttl_seconds: float = Field(default=120.0, gt=0)
    adapter_call_timeout_seconds: float = Field(default=30.0, gt=0)

    # The migrate Job refuses to run as any other database role when set (the owner role).
    migrate_expected_role: str | None = None

    # Startup, schema and hosts.
    schema_wait_seconds: float = Field(default=300.0, gt=0)
    schema_poll_seconds: float = Field(default=2.0, gt=0)
    host_recheck_seconds: float = Field(default=60.0, gt=0)
    host_check_timeout_seconds: float = Field(default=10.0, gt=0)

    @property
    def production(self) -> bool:
        """Whether `MODE=production`."""
        return self.mode == "production"

    @model_validator(mode="after")
    def _validate_trustbridge(self) -> Self:
        """Collect every settings-only problem and raise once (all are fatal)."""
        problems: list[str] = []
        names = [host.name for host in self.github_hosts]
        if len(set(names)) != len(names):
            problems.append("GITHUB_HOSTS names must be unique")
        allow_http = self.allow_insecure_github_urls and not self.production
        if self.allow_insecure_github_urls and self.production:
            problems.append("ALLOW_INSECURE_GITHUB_URLS is refused when MODE=production")
        for host in self.github_hosts:
            problems.extend(host.problems(allow_http=allow_http))
            if host.signer not in self.signers:
                problems.append(f"host {host.name}: signer {host.signer!r} is not in SIGNERS")
        if self.production:
            problems.extend(
                f"signer {name}: the file kind is refused when MODE=production"
                for name, signer in self.signers.items()
                if signer.kind == "file"
            )
            problems.extend(self._production_problems())
        if self.adapter_call_timeout_seconds >= self.lease_ttl_seconds:
            problems.append("ADAPTER_CALL_TIMEOUT_SECONDS must be shorter than LEASE_TTL_SECONDS")
        if self.retry_backoff_max_seconds < self.retry_backoff_base_seconds:
            problems.append("RETRY_BACKOFF_MAX_SECONDS must be >= RETRY_BACKOFF_BASE_SECONDS")
        if problems:
            raise ValueError("; ".join(problems))
        used = {host.signer for host in self.github_hosts}
        for name in sorted(set(self.signers) - used):
            logger.warning("signer %s is configured but no host uses it", name)
        return self

    def _production_problems(self) -> list[str]:
        """Production-only refusals (ADR 0028 invariants 17 and 26)."""
        found: list[str] = []
        if self.run_migrations_on_startup:
            found.append("RUN_MIGRATIONS_ON_STARTUP=true is refused when MODE=production")
        if self.trusted_proxies is None:
            found.append("TRUSTED_PROXIES must be set when MODE=production")
        elif "*" in self.trusted_proxies:
            found.append("TRUSTED_PROXIES=* is refused when MODE=production")
        return found


def _env_name(field: str) -> str:
    """The environment variable name of a field (case-insensitive source, upper-case here)."""
    return field.upper()


def known_env_names(settings_cls: type[Settings] = TrustbridgeSettings) -> set[str]:
    """Every environment variable name `settings_cls` reads (field names and aliases)."""
    names: set[str] = set()
    for field, info in settings_cls.model_fields.items():
        names.add(_env_name(field))
        alias = info.validation_alias
        if isinstance(alias, str):
            names.add(alias.upper())
    return names


def check_environment(
    environ: Mapping[str, str] | None = None,
    settings_cls: type[Settings] = TrustbridgeSettings,
    *,
    cutoff: float = 0.9,
) -> None:
    """Fail on an environment variable that is almost, but not exactly, a setting's name.

    The environment source ignores names that match no field, so `LEASE_TTL_SECOND` would
    silently leave the default in place. Names close to a known one (difflib ratio at least
    `cutoff`) raise `SettingsError`; names that are exactly known or far from all are fine.
    """
    env = os.environ if environ is None else environ
    known = known_env_names(settings_cls)
    problems = []
    for name in sorted(env):
        upper = name.upper()
        if upper in known:
            continue
        close = difflib.get_close_matches(upper, known, n=1, cutoff=cutoff)
        if close:
            problems.append(f"environment variable {name} looks like {close[0]}")
    if problems:
        raise SettingsError("; ".join(problems))


def log_effective_settings(settings: TrustbridgeSettings) -> dict[str, object]:
    """Log (and return) the effective value of every trustbridge field that has a default."""
    base = set(Settings.model_fields)
    dumped = settings.model_dump(mode="json")
    effective = {
        _env_name(name): dumped[name]
        for name, info in TrustbridgeSettings.model_fields.items()
        if name not in base and not info.is_required()
    }
    logger.info("trustbridge effective settings", extra={"effective_settings": effective})
    return effective


def load_trustbridge_settings() -> TrustbridgeSettings:
    """Build the settings from the environment: near-miss check, validation, defaults logged."""
    check_environment()
    settings = TrustbridgeSettings()  # type: ignore[call-arg]  # required fields come from env
    log_effective_settings(settings)
    return settings


@lru_cache
def get_trustbridge_settings() -> TrustbridgeSettings:
    """Return the process-wide cached trustbridge settings (trustbridge code uses this)."""
    return load_trustbridge_settings()
