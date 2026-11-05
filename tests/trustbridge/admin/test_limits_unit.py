"""Admin rate limits (ADR 0033 inv. 14-16) and the typed rejections."""

import ipaddress

import pytest

from tests.trustbridge.admin.helpers import build, problem, token
from tests.trustbridge.fakes import FakeRedis
from tests.trustbridge.helpers import as_redis
from trustbridge.ratelimit import ValkeyWindowCounter

VIEWER = "trustbridge:viewer"


def test_reads_over_the_bucket_get_429_with_retry_after() -> None:
    """Inv. 14: 429 problem+json, `Retry-After` of at least one second."""
    client = build(admin_rate_limit_read_requests=2).client(VIEWER)
    assert client.get("/admin/v1/hosts").status_code == 200
    assert client.get("/admin/v1/hosts").status_code == 200
    limited = client.get("/admin/v1/hosts")
    problem(limited, 429, "rate-limited")
    assert 1 <= int(limited.headers["retry-after"]) <= 3600


def test_the_key_is_the_principal_not_the_address() -> None:
    """Inv. 14: another principal from the same address has its own count."""
    env = build(admin_rate_limit_read_requests=1)
    one, two = env.client(VIEWER, sub="one"), env.client(VIEWER, sub="two")
    assert one.get("/admin/v1/hosts").status_code == 200
    assert two.get("/admin/v1/hosts").status_code == 200
    assert one.get("/admin/v1/hosts").status_code == 429
    assert two.get("/admin/v1/hosts").status_code == 429


def test_rejected_requests_count_against_the_address_bucket() -> None:
    """Decision 5: 30/minute per source address for unauthenticated or rejected requests."""
    env = build(admin_rate_limit_anonymous_requests=2)
    anonymous = env.client()
    anonymous.headers.clear()
    statuses = [anonymous.get("/admin/v1/hosts").status_code for _ in range(4)]
    assert statuses == [401, 401, 429, 429]
    response = anonymous.get("/admin/v1/hosts")
    problem(response, 429, "rate-limited")
    assert "retry-after" in response.headers
    # An address with its own count is not affected, and neither is an accepted request.
    other = build(admin_rate_limit_anonymous_requests=2).client(VIEWER, host="198.51.100.9")
    assert other.get("/admin/v1/hosts").status_code == 200
    assert env.client(VIEWER).get("/admin/v1/hosts").status_code == 200


def test_denied_requests_count_too() -> None:
    """A 403 is a rejected request."""
    env = build(admin_rate_limit_anonymous_requests=1)
    client = env.client()
    assert client.get("/admin/v1/hosts").status_code == 403
    assert client.get("/admin/v1/hosts").status_code == 429


def test_an_exempt_address_is_never_limited() -> None:
    """Inv. 14."""
    env = build(
        admin_rate_limit_anonymous_requests=1,
        admin_rate_limit_exempt_cidrs=[ipaddress.ip_network("203.0.113.0/24")],
    )
    client = env.client(host="203.0.113.7")
    client.headers.clear()
    assert [client.get("/admin/v1/hosts").status_code for _ in range(5)] == [401] * 5


def test_replicas_sharing_valkey_share_the_count() -> None:
    """Inv. 16: two apps over one counter store."""
    valkey = FakeRedis()
    first = build(counter=ValkeyWindowCounter(as_redis(valkey)), admin_rate_limit_read_requests=2)
    second = build(counter=ValkeyWindowCounter(as_redis(valkey)), admin_rate_limit_read_requests=2)
    assert first.client(VIEWER).get("/admin/v1/hosts").status_code == 200
    assert second.client(VIEWER).get("/admin/v1/hosts").status_code == 200
    assert first.client(VIEWER).get("/admin/v1/hosts").status_code == 429


def test_valkey_down_serves_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inv. 15: fail open, and the fail-open counter moves."""
    from trustbridge.admin import metrics  # noqa: PLC0415

    added: list[int] = []
    monkeypatch.setattr(metrics.rate_limit_failopen, "add", lambda n, *a, **k: added.append(n))
    valkey = FakeRedis()
    valkey.down = True
    env = build(counter=ValkeyWindowCounter(as_redis(valkey)), admin_rate_limit_read_requests=1)
    client = env.client(VIEWER)
    assert [client.get("/admin/v1/hosts").status_code for _ in range(3)] == [200, 200, 200]
    assert len(added) == 3


def test_disabled_limits_never_reject() -> None:
    """`ADMIN_RATE_LIMIT_ENABLED=false`."""
    client = build(admin_rate_limit_enabled=False, admin_rate_limit_read_requests=1).client(VIEWER)
    assert [client.get("/admin/v1/hosts").status_code for _ in range(3)] == [200] * 3


def test_token_helper_shape() -> None:
    """The fake token carries the client-role claim the real validator would."""
    assert token("s", "a")["Authorization"].startswith("Bearer {")
