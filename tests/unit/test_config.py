"""Unit test: Settings.database_url assembles the async Postgres DSN, plus its
mode-dependent validation (production requires public_base_url/non-default
credentials/https issuer/authenticated MQTT, mock requires allow_mock_mode).
"""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.config import Settings

_PRODUCTION_KWARGS: dict[str, object] = {
    "mode": "production",
    "oidc_audience": "api",
    "postgres_password": "real-password",
    "RUSTFS_ACCESS_KEY": "real-access-key",
    "RUSTFS_SECRET_KEY": "real-secret-key",
    "oidc_issuer_url": "https://issuer.example.com/realms/prod",
    "public_base_url": "https://api.example.com",
    "mqtt_use_tls": True,
    "mqtt_username": "real-mqtt-user",
    "mqtt_password": "real-mqtt-password",
    "s3_endpoint_url": "https://s3.example.com",
    "redis_url": "rediss://redis.example.com:6379/0",
    "postgres_sslmode": "verify-full",
    "subscriber_secret": "production-test-subscriber-key-0123456789",
}


@pytest.fixture(autouse=True)
def _clear_oidc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the devcontainer's http:// OIDC_* settings, which would override production kwargs."""
    for name in ("OIDC_ISSUER", "OIDC_ISSUER_URL", "OIDC_DISCOVERY_URL"):
        monkeypatch.delenv(name, raising=False)


def test_database_url_assembles_dsn_from_pieces() -> None:
    """database_url combines user/password/host/port/db into one asyncpg DSN."""
    settings = Settings(
        postgres_user="u",
        postgres_password="p",  # noqa: S106 -- test fixture value, not a real secret
        postgres_host="h",
        postgres_port=1234,
        postgres_db="d",
    )
    assert settings.database_url == "postgresql+asyncpg://u:p@h:1234/d"


def test_production_mode_requires_public_base_url() -> None:
    """Settings(mode="production") with no public_base_url raises (RFC 9728 needs one)."""
    kwargs = {k: v for k, v in _PRODUCTION_KWARGS.items() if k != "public_base_url"}
    with pytest.raises(ValidationError, match="public_base_url must be set"):
        Settings(**kwargs)  # type: ignore[arg-type]


def test_production_mode_requires_https_public_base_url() -> None:
    """An http:// public base URL is refused in production."""
    kwargs = {**_PRODUCTION_KWARGS, "public_base_url": "http://api.example.com"}
    with pytest.raises(ValidationError, match="public_base_url must use https"):
        Settings(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("url", ["https://api.example.com/v1", "ftp://x", "https://a?b=1", "api"])
def test_public_base_url_must_be_an_origin(url: str) -> None:
    """A path, query or non-http(s) scheme is not an origin."""
    with pytest.raises(ValidationError, match="http\\(s\\) origin"):
        Settings(public_base_url=url)


def test_production_mode_with_full_config_succeeds() -> None:
    """A fully-configured production Settings (non-default creds, https issuer) constructs fine."""
    settings = Settings(**_PRODUCTION_KWARGS)  # type: ignore[arg-type]
    assert settings.oidc_audience == "api"
    assert settings.resource_metadata_url == (
        "https://api.example.com/.well-known/oauth-protected-resource"
    )


def test_oidc_audience_defaults_on_and_cannot_be_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    """The aud check has a default value and no way to switch it off."""
    monkeypatch.delenv("OIDC_AUDIENCE", raising=False)
    assert Settings().oidc_audience == "api"
    with pytest.raises(ValidationError, match="oidc_audience"):
        Settings(oidc_audience="")
    with pytest.raises(ValidationError, match="oidc_audience"):
        Settings(oidc_audience=None)  # type: ignore[arg-type]


def test_legacy_issuer_url_alone_drives_issuer_and_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only OIDC_ISSUER_URL set: expected issuer and discovery URL both derive from it."""
    for name in ("OIDC_ISSUER", "OIDC_DISCOVERY_URL"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(oidc_issuer_url="http://idp.test/realms/r/")
    assert settings.expected_issuer == "http://idp.test/realms/r/"
    assert settings.discovery_url == "http://idp.test/realms/r/.well-known/openid-configuration"


def test_issuer_and_discovery_url_are_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    """OIDC_ISSUER sets the expected `iss`; OIDC_DISCOVERY_URL sets where discovery is fetched."""
    monkeypatch.delenv("OIDC_DISCOVERY_URL", raising=False)
    settings = Settings(oidc_issuer="http://localhost:8080/realms/r")
    assert (
        settings.discovery_url == "http://localhost:8080/realms/r/.well-known/openid-configuration"
    )
    settings = Settings(
        oidc_issuer="http://localhost:8080/realms/r",
        oidc_discovery_url="http://keycloak:8080/realms/r/.well-known/openid-configuration",
    )
    assert settings.expected_issuer == "http://localhost:8080/realms/r"
    assert settings.discovery_url.startswith("http://keycloak:8080/")


def test_production_mode_requires_https_discovery_url() -> None:
    """An http:// discovery URL is refused in production even with an https issuer."""
    kwargs = {**_PRODUCTION_KWARGS, "oidc_discovery_url": "http://keycloak:8080/x"}
    with pytest.raises(ValidationError, match="oidc_discovery_url must use https"):
        Settings(**kwargs)  # type: ignore[arg-type]


def _security_txt(**overrides: object) -> dict[str, object]:
    expires = datetime.now(UTC) + timedelta(days=180)
    return {
        "security_txt_contact": "https://github.com/craftainer/x/security/advisories/new",
        "security_txt_expires": expires,
        **overrides,
    }


def test_security_txt_off_without_contact() -> None:
    """No contact: nothing to validate, nothing served."""
    assert Settings().security_txt_contact is None


def test_security_txt_valid_config_constructs() -> None:
    """A contact with a near-term, zoned Expires is accepted."""
    assert Settings(**_security_txt()).security_txt_contact is not None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"security_txt_expires": None}, "security_txt_expires must be set"),
        ({"security_txt_expires": datetime(2099, 1, 1, tzinfo=UTC)}, "at most one year"),
        ({"security_txt_expires": datetime(2020, 1, 1, tzinfo=UTC)}, "in the future"),
        ({"security_txt_expires": datetime(2099, 1, 1)}, "time zone"),
        ({"security_txt_contact": "http://insecure.example"}, "https:, mailto: or tel:"),
        (
            {"security_txt_policy": "http://p.example"},
            "security_txt_policy and security_txt_canonical must be https",
        ),
        (
            {"security_txt_canonical": "ftp://c.example"},
            "security_txt_policy and security_txt_canonical must be https",
        ),
        ({"security_txt_preferred_languages": "en\nContact: evil"}, "single line"),
    ],
)
def test_security_txt_rejects_bad_config(overrides: dict[str, object], message: str) -> None:
    """Missing/out-of-range Expires, weak URIs and multi-line values refuse to start."""
    with pytest.raises(ValidationError, match=message):
        Settings(**_security_txt(**overrides))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "default"),
    [
        ("postgres_password", "app"),
        ("RUSTFS_ACCESS_KEY", "rustfsadmin"),
        ("RUSTFS_SECRET_KEY", "rustfsadmin"),
    ],
)
def test_production_mode_rejects_default_credentials(field: str, default: str) -> None:
    """Settings(mode="production") with any credential left at its default raises."""
    kwargs = {**_PRODUCTION_KWARGS, field: default}
    with pytest.raises(ValidationError, match="must not be left at their default"):
        Settings(**kwargs)  # type: ignore[arg-type]


def test_dev_mode_allows_default_credentials() -> None:
    """Settings(mode="dev") (the default) keeps every credential at its local default."""
    settings = Settings()
    assert settings.postgres_password == "app"  # noqa: S105 -- asserting the local default
    assert settings.s3_access_key == "rustfsadmin"


def test_production_mode_requires_https_oidc_issuer() -> None:
    """Settings(mode="production") with an http:// oidc_issuer_url raises."""
    kwargs = {**_PRODUCTION_KWARGS, "oidc_issuer_url": "http://issuer.example.com/realms/prod"}
    with pytest.raises(ValidationError, match="oidc_issuer_url"):
        Settings(**kwargs)  # type: ignore[arg-type]


def test_dev_mode_allows_http_oidc_issuer() -> None:
    """Settings(mode="dev") (the default) keeps the local http:// issuer URL default."""
    assert Settings().oidc_issuer_url.startswith("http://")


@pytest.mark.parametrize(
    "override",
    [
        {"mqtt_use_tls": False},
        {"mqtt_username": None},
        {"mqtt_password": None},
    ],
)
def test_production_mode_requires_authenticated_mqtt(override: dict[str, object]) -> None:
    """Settings(mode="production") with TLS off or no username/password raises."""
    kwargs = {**_PRODUCTION_KWARGS, **override}
    with pytest.raises(ValidationError, match="mqtt_use_tls, mqtt_username, and mqtt_password"):
        Settings(**kwargs)  # type: ignore[arg-type]


def test_dev_mode_allows_unauthenticated_mqtt() -> None:
    """Settings(mode="dev") (the default) keeps the local no-auth/no-TLS MQTT defaults."""
    settings = Settings()
    assert settings.mqtt_use_tls is False
    assert settings.mqtt_username is None
    assert settings.mqtt_password is None


def test_production_mode_requires_https_s3_endpoint() -> None:
    """Settings(mode="production") with an http:// s3_endpoint_url raises."""
    kwargs = {**_PRODUCTION_KWARGS, "s3_endpoint_url": "http://s3.example.com"}
    with pytest.raises(ValidationError, match="s3_endpoint_url"):
        Settings(**kwargs)  # type: ignore[arg-type]


def test_dev_mode_allows_http_s3_endpoint() -> None:
    """Settings(mode="dev") (the default) keeps the local http:// S3 endpoint default."""
    assert Settings().s3_endpoint_url.startswith("http://")


def test_production_mode_requires_encrypted_redis_url() -> None:
    """Settings(mode="production") with a plain redis:// URL raises."""
    kwargs = {**_PRODUCTION_KWARGS, "redis_url": "redis://redis.example.com:6379/0"}
    with pytest.raises(ValidationError, match="redis_url"):
        Settings(**kwargs)  # type: ignore[arg-type]


def test_dev_mode_allows_plain_redis_url() -> None:
    """Settings(mode="dev") (the default) keeps the local redis:// URL default."""
    assert Settings().redis_url.startswith("redis://")


@pytest.mark.parametrize("sslmode", [None, "disable", "allow", "prefer"])
def test_production_mode_requires_encrypting_postgres_sslmode(sslmode: str | None) -> None:
    """Settings(mode="production") with an unset or non-encrypting sslmode raises."""
    kwargs = {**_PRODUCTION_KWARGS, "postgres_sslmode": sslmode}
    with pytest.raises(ValidationError, match="postgres_sslmode"):
        Settings(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("sslmode", ["require", "verify-ca", "verify-full"])
def test_production_mode_accepts_encrypting_postgres_sslmode(sslmode: str) -> None:
    """require, verify-ca and verify-full satisfy the production guardrail."""
    settings = Settings(**{**_PRODUCTION_KWARGS, "postgres_sslmode": sslmode})  # type: ignore[arg-type]
    assert settings.postgres_sslmode == sslmode


def test_dev_mode_allows_unset_postgres_sslmode() -> None:
    """Settings(mode="dev") (the default) keeps postgres_sslmode unset (no TLS)."""
    assert Settings().postgres_sslmode is None


def test_postgres_sslmode_rejects_unknown_value() -> None:
    """A value outside libpq's sslmode set is rejected."""
    with pytest.raises(ValidationError, match="postgres_sslmode"):
        Settings(postgres_sslmode="bogus")  # type: ignore[arg-type]


def test_db_settings_defaults_keep_todays_behavior() -> None:
    """Every new DB setting defaults to today's behavior (nothing consumes them yet)."""
    settings = Settings()
    assert settings.db_pool_mode == "null"
    assert settings.db_pool_size is None
    assert settings.db_max_overflow is None
    assert settings.db_pool_recycle is None
    assert settings.db_connect_timeout is None
    assert settings.db_command_timeout is None
    assert settings.run_migrations_on_startup is True


def test_db_settings_accept_valid_values() -> None:
    """Valid pool/timeout values are accepted as given."""
    settings = Settings(
        db_pool_mode="local",
        db_pool_size=5,
        db_max_overflow=0,
        db_pool_recycle=1800,
        db_connect_timeout=2.5,
        db_command_timeout=30,
        run_migrations_on_startup=False,
    )
    assert settings.db_pool_mode == "local"
    assert settings.db_pool_size == 5
    assert settings.db_connect_timeout == 2.5
    assert settings.run_migrations_on_startup is False


@pytest.mark.parametrize(
    "override",
    [
        {"db_pool_mode": "bogus"},
        {"db_pool_size": -1},
        {"db_max_overflow": -1},
        {"db_pool_recycle": -1},
        {"db_connect_timeout": 0},
        {"db_command_timeout": -1.0},
    ],
)
def test_db_settings_reject_invalid_values(override: dict[str, object]) -> None:
    """Unknown pool modes, negative sizes and non-positive timeouts are rejected."""
    with pytest.raises(ValidationError):
        Settings(**override)  # type: ignore[arg-type]


def test_unrelated_env_vars_are_not_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """extra="forbid" only rejects a value passed for a key that maps to no declared field.

    pydantic-settings' environment source only ever reads env vars matching a
    field's own name/validation_alias -- it never scans the rest of the process
    environment -- so an arbitrary, unrelated env var (or a typo of a real one,
    e.g. OIDC_AUDIANCE instead of OIDC_AUDIENCE) is simply absent from what the
    environment source hands to Settings, "forbid" or "ignore" alike.
    """
    monkeypatch.setenv("SOME_UNRELATED_ENV_VAR", "anything")
    monkeypatch.setenv("OIDC_AUDIANCE", "api")
    Settings()


def test_unrecognized_constructor_keyword_is_rejected() -> None:
    """extra="forbid" rejects an unrecognized key passed directly to the constructor."""
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Settings(this_field_does_not_exist="x")  # type: ignore[call-arg]


def test_mock_mode_requires_allow_mock_mode_flag() -> None:
    """Settings(mode="mock") with no allow_mock_mode raises ValidationError."""
    with pytest.raises(ValidationError, match="ALLOW_MOCK_MODE"):
        Settings(mode="mock")


def test_mock_mode_with_allow_mock_mode_succeeds() -> None:
    """Settings(mode="mock", allow_mock_mode=True) constructs without error."""
    settings = Settings(mode="mock", allow_mock_mode=True)
    assert settings.mode == "mock"


def test_stats_cache_ttl_is_off_by_default_and_accepts_positive_seconds() -> None:
    """STATS_CACHE_TTL_SECONDS unset disables the shared stats cache; it must be positive."""
    assert Settings().stats_cache_ttl_seconds is None
    assert Settings(stats_cache_ttl_seconds=30).stats_cache_ttl_seconds == 30
    with pytest.raises(ValidationError, match="stats_cache_ttl_seconds"):
        Settings(stats_cache_ttl_seconds=0)


def test_gc_freeze_health_info_and_profiling_defaults_and_overrides() -> None:
    """gc_freeze is on; /health/info and profiling are off by default, each overridable."""
    defaults = Settings()
    assert defaults.gc_freeze is True
    assert defaults.health_info_enabled is False
    assert defaults.profiling_enabled is False
    custom = Settings(gc_freeze=False, health_info_enabled=True, profiling_enabled=True)
    assert custom.gc_freeze is False
    assert custom.health_info_enabled is True
    assert custom.profiling_enabled is True


def test_subscriber_secret_is_optional_but_must_be_a_real_key() -> None:
    """SUBSCRIBER_SECRET is unset by default; a set one must be at least 32 characters."""
    assert Settings().subscriber_secret is None
    assert Settings(subscriber_secret="k" * 32).subscriber_secret == "k" * 32
    with pytest.raises(ValidationError, match="subscriber_secret"):
        Settings(subscriber_secret="short")  # noqa: S106 -- a too-short test value, not a secret


def test_production_mode_requires_subscriber_secret() -> None:
    """Settings(mode="production") with no subscriber_secret, or a short one, raises."""
    kwargs = {k: v for k, v in _PRODUCTION_KWARGS.items() if k != "subscriber_secret"}
    with pytest.raises(ValidationError, match="subscriber_secret must be set"):
        Settings(**kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="subscriber_secret"):
        Settings(**{**kwargs, "subscriber_secret": "short"})  # type: ignore[arg-type]
