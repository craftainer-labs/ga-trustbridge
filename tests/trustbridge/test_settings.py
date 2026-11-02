"""Settings hygiene and startup refusals (0028 invariants 17, 19, 26; 0030 invariants 3 and 11)."""

import json
import logging
from typing import Any

import pytest
from pydantic import ValidationError

from tests.trustbridge.helpers import SIGNERS, host, host_dict, make_settings
from trustbridge.github.hosts import DEFAULT_HOST_RECHECK_SECONDS
from trustbridge.settings import (
    SettingsError,
    TrustbridgeSettings,
    check_environment,
    get_trustbridge_settings,
    load_trustbridge_settings,
    log_effective_settings,
)


def _prod(**overrides: Any) -> TrustbridgeSettings:  # noqa: ANN401
    """Production settings that satisfy the template's own production validators."""
    values: dict[str, Any] = {
        "mode": "production",
        "allow_mock_mode": False,
        "public_base_url": "https://tb.example.com",
        "subscriber_secret": "s" * 32,
        "postgres_password": "not-the-default",
        "RUSTFS_ACCESS_KEY": "ak",
        "RUSTFS_SECRET_KEY": "sk",
        "s3_endpoint_url": "https://s3.example.com",
        "redis_url": "rediss://valkey.example.com:6379/0",
        "postgres_sslmode": "require",
        "oidc_issuer_url": "https://idp.example.com/realms/x",
        "mqtt_use_tls": True,
        "mqtt_username": "u",
        "mqtt_password": "p",
        "run_migrations_on_startup": False,
        "trusted_proxies": ["10.0.0.0/8"],
        "signers": {
            "prod": {
                "kind": "vault-transit",
                "addr": "https://vault.example",
                "key": "k",
                "auth": {"method": "kubernetes", "role": "trustbridge"},
            }
        },
    }
    values.update(overrides)
    hosts = values.pop("hosts", [host(signer="prod")])
    return make_settings(hosts, **values)


def test_production_baseline_is_valid() -> None:
    """Production baseline is valid."""
    assert _prod().production


def test_production_refuses_startup_migrations() -> None:  # 0028 invariant 17
    """Production refuses startup migrations."""
    with pytest.raises(ValidationError, match="RUN_MIGRATIONS_ON_STARTUP=true is refused"):
        _prod(run_migrations_on_startup=True)


@pytest.mark.parametrize("value", [None, ["*"]])
def test_production_refuses_unset_or_wildcard_trusted_proxies(
    value: object,
) -> None:  # 0028 inv. 26
    """Production refuses unset or wildcard trusted proxies."""
    with pytest.raises(ValidationError, match="TRUSTED_PROXIES"):
        _prod(trusted_proxies=value)


def test_production_allows_an_explicitly_empty_trusted_proxies() -> None:
    """Production allows an explicitly empty trusted proxies."""
    assert _prod(trusted_proxies=[]).trusted_proxies == []


def test_trusted_proxies_parse_from_env_forms(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trusted proxies parse from env forms."""
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.0/8, 192.168.0.1")
    assert make_settings().trusted_proxies == ["10.0.0.0/8", "192.168.0.1"]
    monkeypatch.setenv("TRUSTED_PROXIES", '["172.16.0.0/12"]')
    assert make_settings().trusted_proxies == ["172.16.0.0/12"]
    monkeypatch.setenv("TRUSTED_PROXIES", "*")
    assert make_settings().trusted_proxies == ["*"]


def test_non_production_may_leave_trusted_proxies_unset() -> None:
    """Non production may leave trusted proxies unset."""
    assert make_settings().trusted_proxies is None


def test_github_hosts_and_signers_have_no_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Github hosts and signers have no default."""
    monkeypatch.delenv("GITHUB_HOSTS", raising=False)
    monkeypatch.delenv("SIGNERS", raising=False)
    with pytest.raises(ValidationError) as excinfo:
        TrustbridgeSettings(mode="mock", allow_mock_mode=True)  # type: ignore[call-arg]
    missing = {e["loc"][0] for e in excinfo.value.errors() if e["type"] == "missing"}
    assert {"github_hosts", "signers"} <= missing


def test_hosts_and_signers_load_from_env_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosts and signers load from env json."""
    monkeypatch.setenv(
        "GITHUB_HOSTS",
        '[{"name":"github.com","kind":"github.com","web_url":"https://github.com",'
        '"api_url":"https://api.github.com","oidc_issuer":"https://x","registry_host":"ghcr.io",'
        '"signer":"dev"}]',
    )
    monkeypatch.setenv("SIGNERS", '{"dev":{"kind":"file","path":"/p.pem"}}')
    monkeypatch.setenv("MODE", "mock")
    monkeypatch.setenv("ALLOW_MOCK_MODE", "1")
    loaded = TrustbridgeSettings()  # type: ignore[call-arg]
    assert loaded.github_hosts[0].app_id is None
    assert not loaded.github_hosts[0].registered


@pytest.mark.parametrize(
    ("kind", "expected"), [("github.com", True), ("ghes", False), ("ghe.com", False)]
)
def test_oidc_id_claims_default_by_kind_and_explicit_wins(kind: str, expected: bool) -> None:
    """Oidc id claims default by kind and explicit wins."""
    api = {
        "github.com": "https://api.github.com",
        "ghes": "https://ghes.example.com/api/v3",
        "ghe.com": "https://api.acme.ghe.com",
    }[kind]
    assert host(kind=kind, api_url=api).oidc_id_claims is expected
    assert host(kind=kind, api_url=api, oidc_id_claims=not expected).oidc_id_claims is not expected


def test_unknown_host_kind_is_rejected() -> None:
    """Unknown host kind is rejected."""
    with pytest.raises(ValidationError, match="unknown kind"):
        make_settings(
            [{k: v for k, v in host_dict(kind="gitlab").items() if k != "oidc_id_claims"}]
        )
    with pytest.raises(ValidationError, match="invalid field"):
        make_settings([host_dict(kind="gitlab", oidc_id_claims=False)])


@pytest.mark.parametrize(
    ("hosts", "message"),
    [
        ([host_dict(signer="missing")], "is not in SIGNERS"),
        ([host_dict(web_url="http://github.com")], "web_url must be an https URL"),
        ([host_dict(api_url="https://api.example.com")], "github.com api_url must be"),
        ([host_dict(kind="ghes", api_url="https://ghes.example.com")], "ghes api_url must be"),
        ([host_dict(app_id=None)], "must be set together"),
        ([host_dict(webhook_secret_ref=None)], "must be set together"),
        ([host_dict(), host_dict()], "duplicate host name"),
        ([host_dict("-bad-name")], "must match"),
    ],
)
def test_settings_only_host_problems_are_fatal(
    hosts: list[Any], message: str
) -> None:  # 0030 inv. 3
    """Settings only host problems are fatal."""
    with pytest.raises(ValidationError, match=message):
        make_settings(hosts)


def test_plain_http_needs_the_dev_flag_and_is_refused_in_production() -> None:
    """Plain http needs the dev flag and is refused in production."""
    http_host = host_dict(web_url="http://github.com", api_url="http://api.github.com")
    with pytest.raises(ValidationError, match="must be an https URL"):
        make_settings([http_host])
    assert make_settings([http_host], allow_insecure_github_urls=True)
    with pytest.raises(ValidationError, match="ALLOW_INSECURE_GITHUB_URLS is refused"):
        _prod(hosts=[{**http_host, "signer": "prod"}], allow_insecure_github_urls=True)


def test_file_signer_is_refused_in_production() -> None:
    """File signer is refused in production."""
    with pytest.raises(ValidationError, match="file kind is refused"):
        _prod(signers=SIGNERS, hosts=[host(signer="dev")])


def test_unregistered_host_is_valid_and_unused_signer_only_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unregistered host is valid and unused signer only warns."""
    unregistered = host_dict(app_id=None, webhook_secret_ref=None)
    with caplog.at_level(logging.WARNING):
        s = make_settings([unregistered], signers={**SIGNERS, "spare": SIGNERS["dev"]})
    assert not s.github_hosts[0].registered
    assert "spare" in caplog.text


def test_adapter_timeout_must_be_shorter_than_the_lease_ttl() -> None:  # 0028 invariant 11
    """Adapter timeout must be shorter than the lease ttl."""
    with pytest.raises(ValidationError, match="shorter than LEASE_TTL_SECONDS"):
        make_settings(lease_ttl_seconds=30, adapter_call_timeout_seconds=30)


def test_retry_backoff_bounds_are_ordered() -> None:
    """Retry backoff bounds are ordered."""
    with pytest.raises(ValidationError, match="RETRY_BACKOFF_MAX_SECONDS"):
        make_settings(retry_backoff_base_seconds=10, retry_backoff_max_seconds=1)


def test_documented_defaults() -> None:  # ADR 0028 item 9 and the limiter section
    """Documented defaults."""
    s = make_settings()
    assert (s.webhook_rate_limit_requests, s.webhook_rate_limit_window_seconds) == (300, 60)
    assert s.webhook_rate_limit_timeout_ms == 50
    assert s.webhook_rate_limit_exempt_cidrs == []
    assert (s.body_retention_processed_days, s.body_retention_dead_days) == (7, 30)
    assert (s.delivery_id_retention_days, s.lease_ttl_seconds) == (30, 120)
    assert (s.adapter_call_timeout_seconds, s.relay_recheck_seconds) == (30, 900)
    assert (s.schema_wait_seconds, s.host_recheck_seconds) == (300, 60)
    assert (s.max_delivery_attempts, s.retry_backoff_base_seconds) == (8, 30)
    assert s.retry_backoff_max_seconds == 1800


def test_near_miss_environment_name_fails_startup() -> None:  # 0028 invariant 19
    """Near miss environment name fails startup."""
    with pytest.raises(SettingsError, match="LEASE_TTL_SECOND looks like LEASE_TTL_SECONDS"):
        check_environment({"LEASE_TTL_SECOND": "60", "PATH": "/bin", "HOME": "/root"})


def test_exact_unrelated_and_aliased_names_pass_the_near_miss_check() -> None:
    """Exact unrelated and aliased names pass the near miss check."""
    check_environment(
        {"LEASE_TTL_SECONDS": "60", "HOME": "/root", "RUSTFS_ACCESS_KEY": "x", "OTEL_X": "1"}
    )


def test_no_dotenv_or_secrets_dir_source() -> None:  # 0028 "Settings"
    """No dotenv or secrets dir source."""
    config = TrustbridgeSettings.model_config
    assert config.get("extra") == "forbid"
    assert config.get("env_file") is None
    assert config.get("secrets_dir") is None


def test_effective_defaults_are_logged(caplog: pytest.LogCaptureFixture) -> None:  # invariant 19
    """Effective defaults are logged."""
    with caplog.at_level(logging.INFO, logger="trustbridge.settings"):
        effective = log_effective_settings(make_settings())
    assert effective["LEASE_TTL_SECONDS"] == 120
    assert effective["WEBHOOK_RATE_LIMIT_REQUESTS"] == 300
    record = next(r for r in caplog.records if r.getMessage() == "trustbridge effective settings")
    assert record.effective_settings["MAX_DELIVERY_ATTEMPTS"] == 8  # type: ignore[attr-defined]
    assert "GITHUB_HOSTS" not in effective  # required fields have no default to report


def test_signers_given_as_json_text_are_parsed() -> None:
    """Signers given as json text are parsed."""
    parsed = make_settings(signers='{"dev":{"kind":"file","path":"/p.pem"}}')
    assert parsed.signers["dev"].kind == "file"


def test_hosts_given_as_parsed_instances_are_kept() -> None:
    """Hosts given as parsed instances are kept."""
    parsed = host()
    assert make_settings([parsed]).github_hosts == [parsed]


def test_host_recheck_default_is_the_host_module_constant() -> None:
    """Host recheck default is the host module constant."""
    assert make_settings().host_recheck_seconds == DEFAULT_HOST_RECHECK_SECONDS


def test_settings_load_from_the_environment_and_are_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`load_trustbridge_settings` validates the environment; the getter caches the result."""
    monkeypatch.setenv("GITHUB_HOSTS", json.dumps([host_dict()]))
    monkeypatch.setenv("SIGNERS", json.dumps(SIGNERS))
    monkeypatch.setenv("MODE", "mock")
    monkeypatch.setenv("ALLOW_MOCK_MODE", "1")
    loaded = load_trustbridge_settings()
    assert loaded.github_hosts[0].name == "github.com"
    get_trustbridge_settings.cache_clear()
    try:
        assert get_trustbridge_settings() is get_trustbridge_settings()
    finally:
        get_trustbridge_settings.cache_clear()
