"""Host configuration, settings checks (fatal tier) and App JWT assembly."""

import base64
import json
from typing import Any

import pytest

from tests.trustbridge.github.conftest import KeySigner
from trustbridge.github.app_jwt import (
    CLOCK_SKEW_SECONDS,
    MAX_LIFETIME_SECONDS,
    build_app_jwt,
    jwt_claims,
    verify_rs256,
)
from trustbridge.github.hosts import (
    HostSettingsError,
    load_hosts,
    validate_hosts,
)
from trustbridge.signer.config import parse_signers

SIGNERS = parse_signers(
    {
        "main": {
            "kind": "vault-transit",
            "addr": "https://vault.example",
            "key": "k",
            "auth": {"method": "token", "token_file": "/t"},
        },
        "dev": {"kind": "file", "path": "/p"},
    }
)


def entry(**over: object) -> dict[str, object]:
    """A valid github.com entry with overrides."""
    base = {
        "name": "github.com",
        "kind": "github.com",
        "web_url": "https://github.com",
        "api_url": "https://api.github.com",
        "oidc_issuer": "https://token.actions.githubusercontent.com",
        "registry_host": "ghcr.io",
        "signer": "main",
    }
    return base | over


def test_oidc_id_claims_default_depends_on_kind_and_explicit_wins() -> None:
    """0030: True for github.com, False for ghes and ghe.com; an explicit value always wins."""
    ghes = entry(
        name="g", kind="ghes", web_url="https://ghe.corp", api_url="https://ghe.corp/api/v3"
    )
    com = entry(
        name="c",
        kind="ghe.com",
        web_url="https://acme.ghe.com",
        api_url="https://api.acme.ghe.com",
    )
    hosts = load_hosts([entry(), ghes, com, entry(name="x", oidc_id_claims=False)])
    assert [h.oidc_id_claims for h in hosts] == [True, False, False, False]
    assert load_hosts([dict(ghes, oidc_id_claims=True)])[0].oidc_id_claims is True


def test_hosts_load_from_json_and_project_to_the_adapter_host() -> None:
    """`to_host` carries exactly the adapter-facing fields."""
    host = load_hosts(json.dumps([entry(**{"app_id": 5, "webhook_secret_ref": "r"})]))[0]
    assert host.registered
    projected = host.to_host()
    assert (projected.name, projected.kind, projected.registry_host) == (
        "github.com",
        "github.com",
        "ghcr.io",
    )


def test_unregistered_entries_may_omit_app_id_and_secret_ref() -> None:
    """An entry without an App is valid and unregistered."""
    assert load_hosts([entry()])[0].registered is False


@pytest.mark.parametrize(
    "bad",
    [
        entry(app_id=5),
        entry(**{"webhook_secret_ref": "r"}),
        entry(kind="gitlab"),
        entry(surprise=1),
    ],
)
def test_invalid_entries_stop_startup(bad: dict[str, Any]) -> None:
    """Invariant 3: app_id without a secret ref (or reverse), unknown kind, unknown field."""
    with pytest.raises(HostSettingsError):
        load_hosts([bad])


@pytest.mark.parametrize(
    ("override", "production", "needle"),
    [
        ({"signer": "nope"}, False, "not in SIGNERS"),
        ({"signer": "dev"}, True, "file signer"),
        ({"web_url": "http://github.com"}, False, "https"),
        ({"api_url": "https://api.example.com"}, False, "api.github.com"),
        (
            {"kind": "ghes", "web_url": "https://ghe.corp", "api_url": "https://ghe.corp"},
            False,
            "/api/v3",
        ),
        (
            {
                "kind": "ghe.com",
                "web_url": "https://acme.ghe.com",
                "api_url": "https://ghe.com/api",
            },
            False,
            "api.SUB.ghe.com",
        ),
    ],
)
def test_settings_checks_are_fatal(override: dict[str, Any], production: bool, needle: str) -> None:
    """Invariant 3: each settings error raises, naming the problem."""
    with pytest.raises(HostSettingsError, match=needle):
        validate_hosts(load_hosts([entry(**override)]), SIGNERS, production=production)


def test_dev_mode_allows_http_and_unused_signers_only_warn() -> None:
    """The flagged dev mode relaxes https; a signer no host uses is a warning."""
    hosts = load_hosts([entry(web_url="http://github.com")])
    warnings = validate_hosts(hosts, SIGNERS, production=False, dev_mode=True)
    assert warnings == ["signer 'dev' is not used by any host"]


def test_a_valid_ghes_and_ghe_com_pass() -> None:
    """The documented URL shapes are accepted."""
    hosts = load_hosts(
        [
            entry(
                name="a", kind="ghes", web_url="https://ghe.corp", api_url="https://ghe.corp/api/v3"
            ),
            entry(
                name="b",
                kind="ghe.com",
                web_url="https://acme.ghe.com",
                api_url="https://api.acme.ghe.com",
            ),
        ]
    )
    validate_hosts(hosts, SIGNERS, production=True)


# ---- JWT ------------------------------------------------------------------------------------
def test_claims_never_exceed_the_limits() -> None:
    """Invariant 2: exp at most 9 minutes ahead, iat not later than now, skew covered."""
    now = 1_800_000_000.5
    claims = jwt_claims(77, now)
    assert claims["iat"] == int(now) - CLOCK_SKEW_SECONDS
    assert int(claims["iat"]) <= now
    assert int(claims["exp"]) - now <= MAX_LIFETIME_SECONDS
    assert jwt_claims(77, now, lifetime=3600)["exp"] == int(now) + MAX_LIFETIME_SECONDS
    assert int(jwt_claims(77, now, lifetime=0)["exp"]) > now


async def test_jwt_is_a_verifiable_rs256_token() -> None:
    """Header, claims and a signature that verifies against the signer's public key."""
    signer = KeySigner()
    token = await build_app_jwt(signer, 77, now=1_800_000_000)
    head, body, sig = token.split(".")

    def dec(part: str) -> bytes:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))

    assert json.loads(dec(head)) == {"alg": "RS256", "typ": "JWT"}
    claims = json.loads(dec(body))
    assert claims == {"iat": 1_800_000_000 - 60, "exp": 1_800_000_000 + 540, "iss": 77}
    assert verify_rs256(await signer.public_key_pem(), f"{head}.{body}".encode(), dec(sig))
    assert not verify_rs256(await signer.public_key_pem(), b"other", dec(sig))
