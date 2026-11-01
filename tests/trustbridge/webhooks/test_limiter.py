"""The webhook limiter (0028 invariants 25-28): window, keys, sharing, fail-open, proxies."""

import asyncio
import ipaddress
import time

import httpx
import pytest
from fastapi import FastAPI, Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from tests.trustbridge.fakes import FakeRedis
from tests.trustbridge.helpers import as_redis
from trustbridge.webhooks.limiter import (
    InMemoryWindowCounter,
    ValkeyWindowCounter,
    WebhookLimiter,
    source_key,
)


def _limiter(counter: object, **kw: object) -> WebhookLimiter:
    values: dict[str, object] = {
        "counter": counter,
        "enabled": True,
        "requests": 3,
        "window_seconds": 60,
        "timeout_ms": 50,
        "clock": lambda: 1_000_000.0 + 10,  # 10 s into a 60 s window (1_000_000 // 60 = 16666)
    }
    values.update(kw)
    return WebhookLimiter(**values)  # type: ignore[arg-type]


async def test_over_limit_is_rejected_with_the_seconds_left_in_the_window() -> None:  # inv. 25
    limiter = _limiter(InMemoryWindowCounter())
    for _ in range(3):
        assert (await limiter.check("203.0.113.5")).allowed
    decision = await limiter.check("203.0.113.5")
    window_end = (1_000_010 // 60 + 1) * 60
    assert not decision.allowed
    assert decision.retry_after == window_end - 1_000_010
    assert (await limiter.check("203.0.113.6")).allowed  # another address has its own count


async def test_retry_after_is_at_least_one_second() -> None:
    limiter = _limiter(InMemoryWindowCounter(), requests=1, clock=lambda: 59.9999)
    await limiter.check("a")
    assert (await limiter.check("a")).retry_after == 1


async def test_exempt_cidr_is_never_limited_and_others_are() -> None:  # inv. 25
    limiter = _limiter(
        InMemoryWindowCounter(), requests=1, exempt=[ipaddress.ip_network("192.0.2.0/24")]
    )
    for _ in range(10):
        assert (await limiter.check("192.0.2.77")).allowed
    await limiter.check("198.51.100.1")
    assert not (await limiter.check("198.51.100.1")).allowed


async def test_disabled_limiter_allows_everything() -> None:
    limiter = _limiter(InMemoryWindowCounter(), enabled=False, requests=1)
    for _ in range(5):
        assert (await limiter.check("a")).allowed


def test_keys_ipv6_by_slash_64_missing_client_as_unknown() -> None:  # inv. 26
    assert source_key("2001:db8:1:2:aaaa:bbbb:cccc:dddd") == source_key("2001:db8:1:2::1")
    assert source_key("2001:db8:1:2::1") != source_key("2001:db8:1:3::1")
    assert source_key(None) == "unknown"
    assert source_key("") == "unknown"
    assert source_key("::ffff:203.0.113.9") == "203.0.113.9"
    assert source_key("fe80::1%eth0") == source_key("fe80::2")
    assert source_key("testclient") == "testclient"


async def test_two_api_instances_share_one_valkey_count() -> None:  # inv. 27
    valkey = FakeRedis()
    a = _limiter(ValkeyWindowCounter(as_redis(valkey)), requests=4)
    b = _limiter(ValkeyWindowCounter(as_redis(valkey)), requests=4)
    results = [(await x.check("203.0.113.5")).allowed for x in (a, b, a, b, a)]
    assert results == [True, True, True, True, False]
    key = next(iter(valkey.kv))
    assert key == f"tb:rl:webhooks:203.0.113.5:{1_000_010 // 60}"
    assert valkey.expiries[key] == 120  # twice the window, set on the first increment


async def test_valkey_error_fails_open_and_counts_a_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # inv. 28
    valkey = FakeRedis()
    valkey.down = True
    added: list[int] = []
    monkeypatch.setattr(
        "trustbridge.webhooks.limiter.metrics.limiter_failopen.add", lambda n: added.append(n)
    )
    limiter = _limiter(ValkeyWindowCounter(as_redis(valkey)), requests=1)
    for _ in range(3):
        assert (await limiter.check("203.0.113.5")).allowed
    assert added == [1, 1, 1]


async def test_a_slow_valkey_costs_at_most_the_timeout() -> None:  # inv. 28

    class Slow:
        async def incr(self, key: str, ttl_seconds: int) -> int:
            await asyncio.sleep(5)
            return 1

    limiter = _limiter(Slow(), timeout_ms=50)
    started = time.monotonic()
    decision = await limiter.check("203.0.113.5")
    assert decision.allowed
    assert time.monotonic() - started < 0.5  # 50 ms budget plus scheduling slack


def _echo_app(trusted: list[str] | str) -> FastAPI:
    app = FastAPI()

    @app.get("/who")
    async def who(request: Request) -> dict[str, str | None]:
        return {"key": source_key(request.client.host if request.client else None)}

    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=trusted)
    return app


async def _who(app: FastAPI, peer: str, forwarded: list[str]) -> str:
    transport = httpx.ASGITransport(app=app, client=(peer, 4321))
    headers = [("x-forwarded-for", value) for value in forwarded]
    async with httpx.AsyncClient(transport=transport, base_url="http://tb") as client:
        response = await client.get("/who", headers=headers)
    return str(response.json()["key"])


async def test_forwarded_header_from_an_untrusted_peer_does_not_change_the_key() -> None:  # 26
    app = _echo_app(["10.0.0.0/8"])
    assert await _who(app, "203.0.113.50", ["198.51.100.99"]) == "203.0.113.50"


async def test_trusted_chain_keys_on_the_rightmost_untrusted_entry() -> None:  # inv. 26
    app = _echo_app(["10.0.0.0/8"])
    # client -> 10.1.1.1 (proxy) -> 10.0.0.9 (proxy, the peer); a forged left entry is ignored.
    key = await _who(app, "10.0.0.9", ["6.6.6.6, 198.51.100.7", "10.1.1.1"])
    assert key == "198.51.100.7"


async def test_empty_trusted_list_means_use_the_socket_peer() -> None:  # inv. 26
    assert await _who(_echo_app([]), "10.0.0.9", ["198.51.100.7"]) == "10.0.0.9"
