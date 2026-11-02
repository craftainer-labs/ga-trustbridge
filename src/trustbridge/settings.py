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
from typing import Annotated, Any, Self

from pydantic import Field, model_validator
from pydantic.functional_validators import BeforeValidator
from pydantic_settings import NoDecode, SettingsConfigDict

from app.config import Settings
from trustbridge.github.hosts import (
    DEFAULT_HOST_RECHECK_SECONDS,
    GitHubHostConfig,
    HostSettingsError,
    load_hosts,
    validate_hosts,
)
from trustbridge.signer.config import FileSignerConfig, SignerConfig, parse_signers

logger = logging.getLogger(__name__)

# GitHub documents a 25 MB payload cap (ADR 0028 open issue: not re-fetched in this pass).
DEFAULT_WEBHOOK_MAX_BODY_BYTES = 25 * 1024 * 1024
_HOST_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{0,62}$")

__all__ = [
    "GitHubHostConfig",
    "SettingsError",
    "SignerConfig",
    "TrustbridgeSettings",
    "check_environment",
    "get_trustbridge_settings",
    "known_env_names",
    "load_trustbridge_settings",
    "log_effective_settings",
]


class SettingsError(ValueError):
    """The process environment holds a setting the process must refuse to start with."""


def _load_hosts(value: Any) -> Any:  # noqa: ANN401 -- pydantic hook
    """Parse `GITHUB_HOSTS` (JSON text or decoded entries), applying the kind-dependent default."""
    if isinstance(value, list) and all(isinstance(item, GitHubHostConfig) for item in value):
        return value
    try:
        return load_hosts(value)
    except HostSettingsError as exc:
        raise ValueError(str(exc)) from exc


def _load_signers(value: Any) -> Any:  # noqa: ANN401 -- pydantic hook
    """Parse `SIGNERS` given as JSON text (a decoded mapping goes through normal validation)."""
    return parse_signers(value) if isinstance(value, str) else value


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
    github_hosts: Annotated[list[GitHubHostConfig], NoDecode, BeforeValidator(_load_hosts)]
    signers: Annotated[dict[str, SignerConfig], NoDecode, BeforeValidator(_load_signers)]
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

    # Directory of the writable local secret store (`store:<name>` references: parked values,
    # tenant connection data). Unset: such writes fail closed (ADR 0029 value store).
    secret_store_dir: Path | None = None

    # The migrate Job refuses to run as any other database role when set (the owner role).
    migrate_expected_role: str | None = None

    # Startup, schema and hosts.
    schema_wait_seconds: float = Field(default=300.0, gt=0)
    schema_poll_seconds: float = Field(default=2.0, gt=0)
    host_recheck_seconds: float = Field(default=float(DEFAULT_HOST_RECHECK_SECONDS), gt=0)
    host_check_timeout_seconds: float = Field(default=10.0, gt=0)

    @property
    def production(self) -> bool:
        """Whether `MODE=production`."""
        return self.mode == "production"

    @model_validator(mode="after")
    def _validate_trustbridge(self) -> Self:
        """Collect every settings-only problem and raise once (all are fatal)."""
        problems: list[str] = []
        problems.extend(
            f"host name {host.name!r} must match {_HOST_NAME.pattern}"
            for host in self.github_hosts
            if not _HOST_NAME.match(host.name)
        )
        allow_http = self.allow_insecure_github_urls and not self.production
        if self.allow_insecure_github_urls and self.production:
            problems.append("ALLOW_INSECURE_GITHUB_URLS is refused when MODE=production")
        warnings: list[str] = []
        try:
            # The file signer is refused per signer here (stricter than per used signer).
            warnings = validate_hosts(
                self.github_hosts, self.signers, production=False, dev_mode=allow_http
            )
        except HostSettingsError as exc:
            problems.extend(exc.problems)
        if self.production:
            problems.extend(
                f"signer {name}: the file kind is refused when MODE=production"
                for name, signer in self.signers.items()
                if isinstance(signer, FileSignerConfig)
            )
            problems.extend(self._production_problems())
        if self.adapter_call_timeout_seconds >= self.lease_ttl_seconds:
            problems.append("ADAPTER_CALL_TIMEOUT_SECONDS must be shorter than LEASE_TTL_SECONDS")
        if self.retry_backoff_max_seconds < self.retry_backoff_base_seconds:
            problems.append("RETRY_BACKOFF_MAX_SECONDS must be >= RETRY_BACKOFF_BASE_SECONDS")
        if problems:
            raise ValueError("; ".join(problems))
        for warning in warnings:
            logger.warning(warning)
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
