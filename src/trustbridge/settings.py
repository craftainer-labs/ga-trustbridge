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
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
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


DEFAULT_ADMIN_MAX_BODY_BYTES = 16384
"""The default of `ADMIN_MAX_BODY_BYTES`; `admin.runtime.DEFAULT_MAX_BODY_BYTES` is this value."""
MIN_STATE_KEY_LENGTH = 32
"""The shortest `REGISTRATION_STATE_KEY` accepted."""
MAX_DELETION_LEASE_TTL_SECONDS = 3600
"""The longest `DELETION_LEASE_TTL_SECONDS` (the database functions' bound)."""
MAX_DELETION_DELAY_SECONDS = 86400
"""The longest `DELETION_BACKOFF_*` delay (the engine's bound)."""
DELETION_LEASE_MARGIN = 0.5
"""The share of the deletion lease TTL that the drain poll plus one step pass may use."""
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
"""The hosts `public_base_url` may reach over plain http (local development only)."""


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


def _split_names(value: Any) -> Any:  # noqa: ANN401 -- pydantic hook
    """Accept `ADAPTERS_ENABLED` as a JSON list or a comma-separated string of adapter names."""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            return json.loads(text)
        return [item.strip() for item in text.split(",") if item.strip()]
    return value


AdapterNames = Annotated[list[str], NoDecode, BeforeValidator(_split_names)]


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
    # A delivery of an admin-disabled tenant is deferred this long and checked again (ADR 0033).
    disabled_tenant_retry_seconds: float = Field(default=15.0, gt=0)
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

    # Audit log (ADR 0032). `0` retention means never purge; the floor guards a typo.
    audit_retention_days: int = Field(default=365, ge=0)
    audit_retention_min_days: int = Field(default=90, ge=1)
    audit_export_lag_seconds: float = Field(default=30.0, gt=0)
    # The longest any transaction of the api, worker or reconciler may stay open (ADR 0032
    # "Export", ADR 0034 B8): the `after_begin` hook runs `SET LOCAL transaction_timeout` with it.
    # Its own setting, validated to be below `audit_export_lag_seconds` (so the export lag
    # guarantee holds); the default is half the default lag. 0 would mean "off" and is refused.
    transaction_timeout_seconds: float = Field(default=15.0, gt=0)
    # The purge duty (ADR 0032 "Retention"): batch size is `tb_audit_purge`'s 1..10000; a run
    # does at most `audit_purge_max_batches` batches (1..10000) and stops starting batches after
    # half `lease_ttl_seconds`, so it ends before its lease could lapse.
    audit_purge_interval_seconds: float = Field(default=3600.0, gt=0)
    audit_purge_batch_size: int = Field(default=1000, ge=1, le=10000)
    audit_purge_max_batches: int = Field(default=100, ge=1, le=10000)

    # Tenant deletion (ADR 0034, NG6h-4a-3). The reconciler's singleton duty polls every
    # `deletion_poll_seconds` and runs at most `deletion_batch_size` claimed deletions per poll.
    # The engine's own lease (`deletion_lease_ttl_seconds`, a fenced database lease per deletion)
    # is separate from `lease_ttl_seconds`; a run ends after `deletion_budget_fraction` of it. A
    # failed attempt backs off `min(cap, base * 2**attempts)`. The drain step re-scans the
    # tenant's writers every `deletion_drain_poll_seconds`, at most `deletion_drain_max_passes`
    # times. `deletion_step_pass_seconds` is the longest one step pass may take between two lease
    # renewals (the steps size their batches to it); `_deletion_timing_problems` keeps the drain
    # poll plus one pass inside half the lease TTL.
    deletion_poll_seconds: float = Field(default=10.0, gt=0)
    deletion_batch_size: int = Field(default=10, ge=1, le=1000)
    deletion_lease_ttl_seconds: int = Field(default=120, ge=1, le=MAX_DELETION_LEASE_TTL_SECONDS)
    deletion_budget_fraction: float = Field(default=0.5, gt=0, lt=1)
    deletion_backoff_base_seconds: int = Field(default=30, ge=1, le=MAX_DELETION_DELAY_SECONDS)
    deletion_backoff_cap_seconds: int = Field(default=3600, ge=1, le=MAX_DELETION_DELAY_SECONDS)
    deletion_drain_max_passes: int = Field(default=30, ge=2, le=10000)
    deletion_drain_poll_seconds: float = Field(default=2.0, ge=0)
    deletion_step_pass_seconds: float = Field(default=30.0, gt=0)

    # Adapters the api process loads for the admin API's capability summaries (ADR 0029: an
    # adapter not listed is never imported). Entry-point names, for example `ghcr,jfrog`.
    adapters_enabled: AdapterNames = Field(default_factory=list)

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
    # Port of the worker's and reconciler's health endpoints (`/health/live`, `/health/ready`).
    # Unset: no endpoint. The api serves its health routes on its own port and ignores this.
    health_port: int | None = Field(default=None, ge=1, le=65535)

    # Admin API rate limits (ADR 0033 decision 5): fixed windows in Valkey, fail open. Reads,
    # mutations and exports are per principal; `anonymous` is per source address for
    # unauthenticated or rejected requests. Starting points to measure with the UI.
    admin_rate_limit_enabled: bool = True
    admin_rate_limit_read_requests: int = Field(default=600, gt=0)
    admin_rate_limit_mutation_requests: int = Field(default=60, gt=0)
    admin_rate_limit_export_requests: int = Field(default=6, gt=0)
    admin_rate_limit_anonymous_requests: int = Field(default=30, gt=0)
    admin_rate_limit_window_seconds: int = Field(default=60, gt=0)
    admin_rate_limit_timeout_ms: int = Field(default=50, gt=0)
    admin_rate_limit_exempt_cidrs: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = Field(
        default_factory=list
    )

    # Host registration through the admin API (ADR 0030 "Registration", NG6e-4): the manifest's
    # URLs come from `public_base_url` (the BFF serves `<base>/admin/github/registration/callback`
    # and forwards code and state to the admin API) and the one-time `state` is signed with
    # `registration_state_key`, shared by every replica. Both unset: the start route is a 503.
    # `public_base_url` is the template's setting (an origin; https and required in production);
    # `_check_public_base_url` below tightens it for the URLs GitHub is given.
    registration_state_key: SecretStr | None = None

    # Largest request body an admin route reads (413 above it, chunked bodies included; NG6e).
    admin_max_body_bytes: int = Field(default=DEFAULT_ADMIN_MAX_BODY_BYTES, gt=0)

    # Bound on `authz.denied` rows (ADR 0032, NG6d): per (subject, reason) and window, the first
    # `burst` denials are recorded in full, the rest are counted and summarised in one row.
    audit_denied_window_seconds: int = Field(default=60, gt=0)
    audit_denied_burst: int = Field(default=5, gt=0)
    audit_denied_timeout_ms: int = Field(default=50, gt=0)

    # Admin API RBAC (ADR 0033). Instance roles are `trustbridge<sep><role>`, tenant roles
    # `trustbridge<sep><host><sep><org><sep><role>`. The separator is configurable because
    # Keycloak's acceptance of `:` in client role names is not confirmed by its docs.
    rbac_alias_template_personas: bool = True
    rbac_role_separator: str = ":"

    @field_validator("public_base_url")
    @classmethod
    def _check_public_base_url(cls, value: str | None) -> str | None:
        """An absolute https origin, no credentials, query or fragment; the slash is dropped.

        The URL becomes the webhook and redirect URLs of the GitHub App manifest, so it must be
        one GitHub can use and a browser can trust: https, except on a loopback host
        (`localhost`, `127.0.0.1`, `::1`) for local development. The template's own validator
        (`app.config`) adds production's https and required rules on top. A trailing slash is
        removed here so every consumer joins paths the same way.
        """
        if value is None:
            return None
        if any(c.isspace() for c in value):
            raise ValueError("public_base_url must not contain whitespace")
        parts = urlsplit(value)
        try:
            host = parts.hostname
            parts.port  # noqa: B018 -- raises ValueError for an invalid port
        except ValueError:
            raise ValueError("public_base_url has an invalid port") from None
        problems = [
            message
            for bad, message in (
                (parts.scheme not in ("http", "https") or not host, "must be an absolute URL"),
                (
                    parts.scheme == "http" and host not in _LOOPBACK_HOSTS,
                    "must use https:// (http only for localhost, 127.0.0.1 or ::1)",
                ),
                (bool(parts.username or parts.password), "must not contain credentials"),
                (parts.path not in ("", "/"), "must be an origin with no path"),
                (bool(parts.query or parts.fragment), "must have no query or fragment"),
            )
            if bad
        ]
        if problems:
            raise ValueError("public_base_url " + "; ".join(problems))
        return value.removesuffix("/")

    @field_validator("registration_state_key")
    @classmethod
    def _check_state_key(cls, value: SecretStr | None) -> SecretStr | None:
        """At least 32 characters: the key signs the registration `state` (checked here rather
        than left to `Field(min_length=...)`, which a `SecretStr | None` union may not apply)."""
        if value is not None and len(value.get_secret_value()) < MIN_STATE_KEY_LENGTH:
            raise ValueError(
                f"REGISTRATION_STATE_KEY must be at least {MIN_STATE_KEY_LENGTH} characters"
            )
        return value

    @field_validator("rbac_role_separator")
    @classmethod
    def _check_separator(cls, value: str) -> str:
        """One printable ASCII character that host names and org logins cannot contain."""
        if len(value) != 1 or not value.isascii() or not value.isprintable() or value == " ":
            raise ValueError("RBAC_ROLE_SEPARATOR must be one printable non-space ASCII character")
        if value.isalnum() or value in ".-":
            raise ValueError("RBAC_ROLE_SEPARATOR must not be a letter, digit, '.' or '-'")
        return value

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
        if 0 < self.audit_retention_days < self.audit_retention_min_days:
            problems.append("AUDIT_RETENTION_DAYS must be 0 or >= AUDIT_RETENTION_MIN_DAYS")
        if self.transaction_timeout_seconds >= self.audit_export_lag_seconds:
            problems.append(
                "TRANSACTION_TIMEOUT_SECONDS must be shorter than AUDIT_EXPORT_LAG_SECONDS"
            )
        timing = self._deletion_timing_problems()
        if self.deletion_backoff_base_seconds > self.deletion_backoff_cap_seconds:
            problems.append("DELETION_BACKOFF_BASE_SECONDS must be <= DELETION_BACKOFF_CAP_SECONDS")
        if self.production:
            problems.extend(timing)  # a lease that can lapse mid-step: fail closed
        else:
            warnings.extend(timing)
        if problems:
            raise ValueError("; ".join(problems))
        for warning in warnings:
            logger.warning(warning)
        return self

    def _deletion_timing_problems(self) -> list[str]:
        """Whether the drain poll plus one step pass fit inside the deletion lease TTL.

        The engine renews its lease before each pass, so the longest gap between two renewals is
        the drain poll plus one pass. It must stay within `DELETION_LEASE_MARGIN` of the TTL, or
        the lease can lapse under a running step and a second holder starts the same step.
        """
        gap = self.deletion_drain_poll_seconds + self.deletion_step_pass_seconds
        limit = self.deletion_lease_ttl_seconds * DELETION_LEASE_MARGIN
        if gap <= limit:
            return []
        return [
            f"DELETION_DRAIN_POLL_SECONDS + DELETION_STEP_PASS_SECONDS ({gap:g} s) must be at "
            f"most half of DELETION_LEASE_TTL_SECONDS ({limit:g} s)"
        ]

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
