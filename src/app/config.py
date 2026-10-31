"""Application settings, sourced entirely from the process environment.

Both `_require_*` validators below are `# pragma: no cover` for tests/e2e
specifically: the live api process only ever starts with a Settings that
already satisfies them (see .devcontainer/compose.yml's MODE=dev/mock), so a
raise from either one would mean the process never came up for tests/e2e to
run against in the first place. tests/unit/test_config.py exercises both
directly and still counts toward its own 95% gate.
"""

from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Mode = Literal["dev", "mock", "production"]
PostgresSslMode = Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]


class Settings(BaseSettings):
    """Typed application settings."""

    # No .env file for the app itself: every value below is meant to come
    # from the process environment, which the compose files set directly
    # (see .devcontainer/compose.yml and stack/*/compose.yml) --
    # some pulled straight from a stack service's own env file,
    # some (DATABASE_URL, s3_access_key/s3_secret_key) assembled below
    # from those raw pieces since Compose can't interpolate a value from
    # one env file into another compose file's own env var. See
    # README.md's "Configuration" section.
    # extra="forbid" (not "ignore"): pydantic-settings' environment source only ever
    # reads env vars matching a field's own name/validation_alias (verified in
    # tests/unit/test_config.py::test_unrelated_env_vars_are_not_rejected), so this
    # doesn't catch a typo'd env var like OIDC_AUDIANCE -- that's silently absent from
    # the environment source's output either way, "forbid" or "ignore". It does still
    # guard the constructor-kwargs path (Settings(**mapping) with an unrecognized key,
    # e.g. from a future config-file source) rather than silently accepting it.
    model_config = SettingsConfigDict(extra="forbid")

    app_name: str = "template-fastapi"

    # dev: debugger enabled (see app.main). mock: every external service
    # (Postgres, Redis, S3, OIDC) is replaced with a local/in-memory fake --
    # see app.crud_1.heroes, app.health_checks, app.oidc. production:
    # debugger disabled. Defaults to "dev" to match this template's other
    # defaults (localhost hosts, etc.), which assume local/devcontainer use
    # unless overridden -- see the Dockerfile's runner stage for the
    # production default.
    mode: Mode = "dev"

    # MODE=mock trusts every bearer token's claims and skips every real
    # external service (see oidc.decode_bearer_token) -- a deployment that
    # somehow ends up running it (a misconfigured env, a copy-pasted compose
    # file) would silently bypass auth entirely. Requiring this second,
    # explicitly-named flag means MODE=mock can never be reached by MODE's
    # own default/typo alone; only .devcontainer/compose.yml and CI set it,
    # both already scoped to local/CI use.
    allow_mock_mode: bool = False

    # A bulk PATCH/DELETE with a technically-non-empty but always-true filter (e.g.
    # id__gte=0) would otherwise still match every row -- see
    # craftainer_crud.controllers.crud_actions, which counts matches before applying either
    # action and refuses to proceed above this threshold.
    bulk_action_max_matched: int = 1000

    # app.rate_limit: slowapi limit-expression syntax ("<count>/<period>"). Applied to
    # POST /mock/token and each resource's bulk update/delete routes -- the
    # unauthenticated-or-destructive surface prioritized by the OWASP hardening pass
    # this was added in, not every route (see app.rate_limit's module docstring for
    # why this stays per-route instead of a blanket middleware).
    rate_limit_mock_token: str = "10/minute"  # noqa: S105 -- a rate-limit expression, not a secret
    rate_limit_bulk_action: str = "20/minute"

    # app.maintenance.purge_archived: how old an archived row (see
    # craftainer_crud.models.mixins.
    # Archivable) must be before that out-of-request-path script hard-deletes it.
    # None (the default) disables purge entirely -- this devcontainer-only stack has
    # no scheduler/worker service to invoke it automatically either way (see
    # app.maintenance's own module docstring); a real deployment sets this and wires
    # `python -m app.maintenance` into its own host/k8s CronJob.
    archive_purge_after_days: int | None = None

    postgres_user: str = "app"
    postgres_password: str = "app"  # noqa: S105 -- local Postgres default, not a real secret
    postgres_db: str = "app"
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    # libpq sslmode, passed through to asyncpg's `ssl` argument by the engine
    # factory. None (the default) means no TLS, as before; production requires
    # an encrypting mode, see _require_postgres_tls_in_production below.
    postgres_sslmode: PostgresSslMode | None = None

    # SQLAlchemy pool tuning. "null" (NullPool, one connection per use) is
    # today's behavior; "local" is an in-process QueuePool; "external" assumes a
    # pooler such as PgBouncer. The three sizes are None (unset) so the engine
    # factory passes only what is set and SQLAlchemy's own defaults apply.
    db_pool_mode: Literal["null", "local", "external"] = "null"
    db_pool_size: int | None = Field(default=None, ge=0)
    db_max_overflow: int | None = Field(default=None, ge=0)
    db_pool_recycle: int | None = Field(default=None, ge=0)

    # asyncpg timeouts in seconds; None (unset) keeps asyncpg's own defaults.
    db_connect_timeout: float | None = Field(default=None, gt=0)
    db_command_timeout: float | None = Field(default=None, gt=0)

    # Whether the api process runs Alembic migrations itself on startup.
    run_migrations_on_startup: bool = True

    # Run `gc.collect(); gc.freeze()` once startup finishes, moving the import-time object
    # graph out of the cyclic GC's generations so collections stop rescanning it (and a
    # forked worker doesn't copy-on-write its pages). A switch so a load test can A/B it.
    gc_freeze: bool = True

    # Expose GET /health/info (version and commit). Off by default: it discloses the version.
    health_info_enabled: bool = False

    # Honor `?profile=1` with a pyinstrument HTML profile (app.main -> install_profiling).
    # Startup fails if this is set under MODE=production.
    profiling_enabled: bool = False

    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = Field(default="rustfsadmin", validation_alias="RUSTFS_ACCESS_KEY")
    s3_secret_key: str = Field(default="rustfsadmin", validation_alias="RUSTFS_SECRET_KEY")

    redis_url: str = "redis://localhost:6379/0"

    # app.cache: how long a cached /stats or /predict response lives, in seconds. None
    # (the default, env STATS_CACHE_TTL_SECONDS unset) turns the shared cache off. Writes
    # invalidate it through the CRUD event stream; this TTL is the safety net.
    stats_cache_ttl_seconds: int | None = Field(default=None, gt=0)

    # craftainer_crud.interfaces.dependency.build_event_sink_provider/
    # build_event_source_provider's real (non-MODE=mock) backend -- see .devcontainer/stack/mqtt/.
    # mqtt_keepalive_seconds is the aiomqtt.Client keepalive passed to both
    # MQTTEventSink and MQTTEventSource. mqtt_username/mqtt_password/mqtt_use_tls
    # default to unset/False -- matching .devcontainer/stack/mqtt/mosquitto.conf's
    # own no-auth/no-TLS local-dev default (see its "Don't" section) -- and are
    # required to be set in production, see _require_mqtt_auth_in_production below.
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_keepalive_seconds: int = 60
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    # craftainer_crud signs the event-stream subscriber ids it mints (a client cannot choose
    # one). Unset (the default) uses a random per-process secret: ids then survive neither a
    # restart nor a second worker. Set the same value (>= 32 characters) on every replica to
    # let a client resume its stream across restarts and replicas. Env SUBSCRIBER_SECRET.
    # Required (and checked) under MODE=production.
    subscriber_secret: str | None = Field(default=None, min_length=32)
    mqtt_use_tls: bool = False

    # craftainer_crud.controllers.crud_router's `GET <prefix>/events` SSE route: how often an
    # idle stream sends a `: keep-alive` comment, so intermediary proxies/load
    # balancers don't time out the connection.
    sse_keepalive_seconds: float = 15.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        """Build the async Postgres DSN from the individual connection pieces."""
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # OIDC: works with any provider supporting Authorization Code + PKCE, not
    # just Keycloak (see src/app/oidc.py). authorization_url/token_url are
    # static so FastAPI can build the Swagger UI login flow at import time,
    # without a network round trip; the JWKS used to validate tokens is
    # instead discovered lazily from the discovery document (see oidc.py), so
    # unit tests that never authenticate never need network access.
    #
    # The expected issuer (the `iss` a token must carry, and what RFC 9728
    # advertises) is a different thing from the URL the backend reaches the
    # provider at: with Keycloak's KC_HOSTNAME set, tokens carry the public
    # `http://localhost:8080/realms/...` while the backend fetches discovery
    # from `http://keycloak:8080/...` inside the compose network. So:
    #   OIDC_ISSUER        expected `iss` (falls back to OIDC_ISSUER_URL)
    #   OIDC_DISCOVERY_URL full `.../.well-known/openid-configuration` URL
    #                      (falls back to `<expected issuer>/.well-known/...`)
    # OIDC_ISSUER_URL is the original single variable and keeps working alone.
    oidc_issuer_url: str = "http://localhost:8080/realms/template-fastapi"
    oidc_issuer: str | None = None
    oidc_discovery_url: str | None = None
    oidc_authorization_url: str = (
        "http://localhost:8080/realms/template-fastapi/protocol/openid-connect/auth"
    )
    # "_token_url" trips ruff's S105 (looks like a hardcoded password); it is a URL.
    oidc_token_url: str = (
        "http://localhost:8080/realms/template-fastapi/protocol/openid-connect/token"  # noqa: S105
    )
    oidc_client_id: str = "api"
    oidc_algorithm: str = "RS256"
    # Always verified (RFC 9700 section 2.3): a token must carry this `aud`. There is
    # no way to switch the check off; set OIDC_AUDIENCE to the value your provider's
    # audience mapper emits (the bundled realm's `api` and `bff` clients both add `api`).
    oidc_audience: str = Field(default="api", min_length=1)
    # Advertised as RFC 9728 `scopes_supported`; the API authorizes by role, not scope.
    # Env OIDC_SCOPES_SUPPORTED is a JSON list.
    oidc_scopes_supported: list[str] = Field(default_factory=lambda: ["openid"])

    # This service's own public origin (scheme + host[:port], no path): the RFC 9728
    # `resource` and the base of the `resource_metadata` URL in WWW-Authenticate. Taken
    # from configuration, never from the request's Host header, which a client controls.
    # Required (and https) under MODE=production; defaults to localhost otherwise.
    public_base_url: str | None = None

    # RFC 9116 security.txt, served at /.well-known/security.txt only when
    # SECURITY_TXT_CONTACT is set. Contact is one URI (https:, mailto: or tel:), e.g.
    # https://github.com/craftainer/<repo>/security/advisories/new. Expires is required
    # alongside it (RFC 3339 with a zone, in the future and at most one year ahead).
    security_txt_contact: str | None = None
    security_txt_expires: datetime | None = None
    security_txt_policy: str | None = None
    security_txt_preferred_languages: str | None = None
    security_txt_canonical: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def expected_issuer(self) -> str:
        """The `iss` a bearer token must carry (and the RFC 9728 authorization server).

        Used verbatim: `iss` is compared as an exact string (RFC 8414 section 2), and some
        providers include a trailing slash in it.
        """
        return self.oidc_issuer or self.oidc_issuer_url

    @computed_field  # type: ignore[prop-decorator]
    @property
    def discovery_url(self) -> str:
        """Where the backend fetches the OIDC discovery document (may be an internal URL)."""
        base = self.expected_issuer.rstrip("/")
        return self.oidc_discovery_url or f"{base}/.well-known/openid-configuration"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def resource_url(self) -> str:
        """This service's public origin, without a trailing slash."""
        return (self.public_base_url or "http://localhost:8000").rstrip("/")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def resource_metadata_url(self) -> str:
        """Absolute URL of the RFC 9728 protected-resource metadata document."""
        return f"{self.resource_url}/.well-known/oauth-protected-resource"

    @model_validator(mode="after")
    def _validate_public_base_url(self) -> Self:
        """Require an origin-only public base URL, https and explicitly set in production.

        The checks are evaluated into one list and raised in one place, so the single
        `# pragma: no cover` raise is the only line tests/e2e cannot reach (its live
        process only ever starts with a valid Settings); tests/unit/test_config.py
        exercises every message.
        """
        url = self.public_base_url
        parts = urlsplit(url or "")
        production = self.mode == "production"
        problems = [
            message
            for bad, message in (
                (
                    url is not None
                    and (
                        parts.scheme not in ("http", "https")
                        or not parts.netloc
                        or parts.path not in ("", "/")
                        or bool(parts.query)
                        or bool(parts.fragment)
                    ),
                    "public_base_url must be an http(s) origin with no path",
                ),
                (production and url is None, "public_base_url must be set when MODE=production"),
                (
                    production and url is not None and not url.startswith("https://"),
                    "public_base_url must use https:// when MODE=production",
                ),
            )
            if bad
        ]
        if problems:  # pragma: no cover -- see docstring
            raise ValueError("; ".join(problems))
        return self

    @model_validator(mode="after")
    def _validate_security_txt(self) -> Self:
        """Check the security.txt fields: a contact needs a valid, near-term Expires.

        An RFC 9116 file without `Expires` is not compliant, and one expiring more than a
        year out (or already past) defeats the point of the field, so a configured contact
        with a missing or out-of-range Expires refuses to start in every mode. Values go
        into a plain-text file line by line, so control characters are rejected. Evaluated
        and raised like `_validate_public_base_url` (one `# pragma: no cover` raise).
        """
        values = {
            "security_txt_contact": self.security_txt_contact,
            "security_txt_policy": self.security_txt_policy,
            "security_txt_preferred_languages": self.security_txt_preferred_languages,
            "security_txt_canonical": self.security_txt_canonical,
        }
        problems = [
            f"{name} must be a single line of text"
            for name, value in values.items()
            if value is not None and (not value.strip() or not value.isprintable())
        ]
        contact = self.security_txt_contact
        expires = self.security_txt_expires
        now = datetime.now(UTC)
        if contact is not None:
            problems += [
                message
                for bad, message in (
                    (
                        not contact.startswith(("https://", "mailto:", "tel:")),
                        "security_txt_contact must be an https:, mailto: or tel: URI",
                    ),
                    (
                        any(
                            v is not None and not v.startswith("https://")
                            for v in (self.security_txt_policy, self.security_txt_canonical)
                        ),
                        "security_txt_policy and security_txt_canonical must be https:// URLs",
                    ),
                    (
                        expires is None,
                        "security_txt_expires must be set when security_txt_contact is",
                    ),
                    (
                        expires is not None and expires.tzinfo is None,
                        "security_txt_expires must carry a time zone",
                    ),
                    (
                        expires is not None
                        and expires.tzinfo is not None
                        and not now < expires <= now + timedelta(days=366),
                        "security_txt_expires must be in the future and at most one year ahead",
                    ),
                )
                if bad
            ]
        if problems:  # pragma: no cover -- see docstring
            raise ValueError("; ".join(problems))
        return self

    @model_validator(mode="after")
    def _require_allow_mock_mode_flag(self) -> Self:
        """Refuse to construct MODE=mock Settings without the explicit allow_mock_mode flag.

        See allow_mock_mode's own docstring for why this second flag exists.
        """
        if self.mode == "mock" and not self.allow_mock_mode:  # pragma: no cover
            raise ValueError("MODE=mock requires ALLOW_MOCK_MODE=1 to be set")
        return self

    @model_validator(mode="after")
    def _require_subscriber_secret_in_production(self) -> Self:
        """Refuse to construct production Settings without a subscriber-id signing key.

        Without one, craftainer_crud signs event-stream subscriber ids with a random
        per-process key, so no client could resume a stream after a restart or on another
        replica. (`subscriber_secret`'s own `min_length` rejects a too-short value.)
        """
        if self.mode == "production" and self.subscriber_secret is None:  # pragma: no cover
            raise ValueError("subscriber_secret must be set when MODE=production")
        return self

    @model_validator(mode="after")
    def _require_non_default_credentials_in_production(self) -> Self:
        """Refuse to construct production Settings with a credential left at its local default.

        postgres_password/s3_access_key/s3_secret_key all default to well-known,
        publicly documented values (see this class's own field defaults above) meant
        for the local/devcontainer stack only -- a production deployment that
        inherits one of them unchanged would be trivially guessable.
        """
        if self.mode == "production":  # pragma: no cover
            defaults = {
                "postgres_password": "app",
                "s3_access_key": "rustfsadmin",
                "s3_secret_key": "rustfsadmin",
            }
            left_at_default = [
                field for field, default in defaults.items() if getattr(self, field) == default
            ]
            if left_at_default:
                raise ValueError(
                    f"{', '.join(left_at_default)} must not be left at their default "
                    "value when MODE=production"
                )
        return self

    @model_validator(mode="after")
    def _require_encrypted_connections_in_production(self) -> Self:
        """Refuse to construct production Settings with a plaintext S3/Redis/Postgres scheme.

        `_require_non_default_credentials_in_production` above only checks whether a
        credential still equals its local default -- it says nothing about whether the
        connection carrying that credential (and the data behind it) is encrypted. An
        operator who sets a real Postgres/Redis/S3 endpoint but leaves it on
        `http://`/`redis://`/`postgresql+asyncpg://` would otherwise send credentials
        and query traffic in cleartext with no startup failure to catch it, unlike
        oidc_issuer_url and MQTT, which already require this below.
        """
        if self.mode == "production":  # pragma: no cover
            insecure = []
            if not self.s3_endpoint_url.startswith("https://"):
                insecure.append("s3_endpoint_url must use https://")
            if not self.redis_url.startswith("rediss://"):
                insecure.append("redis_url must use rediss://")
            if insecure:
                raise ValueError("; ".join(insecure) + " when MODE=production")
        return self

    @model_validator(mode="after")
    def _require_postgres_tls_in_production(self) -> Self:
        """Refuse to construct production Settings with Postgres TLS off or unverified-weak.

        postgres_sslmode unset (or disable/allow/prefer) can leave the connection in
        cleartext, sending credentials and query traffic unencrypted; production
        must pick require, verify-ca or verify-full.
        """
        if self.mode == "production" and self.postgres_sslmode not in (  # pragma: no cover
            "require",
            "verify-ca",
            "verify-full",
        ):
            raise ValueError(
                "postgres_sslmode must be require, verify-ca, or verify-full when MODE=production"
            )
        return self

    @model_validator(mode="after")
    def _require_https_oidc_issuer_in_production(self) -> Self:
        """Refuse to construct production Settings with a non-https issuer or discovery URL.

        decode_bearer_token fetches the OIDC discovery document and JWKS over whatever
        scheme the discovery URL uses -- http:// in production would send that (and, for
        the Authorization Code flow itself, the token exchange) in cleartext. The expected
        issuer is checked too: it is what clients are told to trust (RFC 9728).
        """
        if self.mode == "production":  # pragma: no cover
            if not self.expected_issuer.startswith("https://"):
                raise ValueError(
                    "oidc_issuer_url/oidc_issuer must use https:// when MODE=production"
                )
            if not self.discovery_url.startswith("https://"):
                raise ValueError("oidc_discovery_url must use https:// when MODE=production")
        return self

    @model_validator(mode="after")
    def _require_mqtt_auth_in_production(self) -> Self:
        """Refuse to construct production Settings with an unauthenticated MQTT broker.

        craftainer_crud.interfaces.base.MQTTEventSink/MQTTEventSource connect with no
        credentials/TLS unless these are set, matching mosquitto.conf's own
        no-auth/no-TLS local-dev default (see .devcontainer/stack/mqtt/
        mosquitto.conf's "Don't" section) -- a production broker reachable over
        the network with that default would let anyone read or forge every
        resource's create/update/delete events.
        """
        if self.mode == "production" and (  # pragma: no cover
            not self.mqtt_use_tls or self.mqtt_username is None or self.mqtt_password is None
        ):
            raise ValueError(
                "mqtt_use_tls, mqtt_username, and mqtt_password must all be set "
                "when MODE=production"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached Settings instance."""
    return Settings()
