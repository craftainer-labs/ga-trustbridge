"""Unit test: that limiter.limit(...) actually enforces a limit (against a
throwaway AsyncLimiter/app on in-memory async storage, not the real shared one --
see tests/conftest.py's _disable_rate_limiting).
"""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.rate_limit import AsyncLimiter, limiter


def _client(throwaway_limiter: AsyncLimiter) -> TestClient:
    app = FastAPI()

    @app.get("/limited")
    @throwaway_limiter.limit("1/minute")
    async def limited(request: Request) -> dict[str, bool]:
        return {"ok": True}

    return TestClient(app)


def test_limiter_limit_enforces_the_configured_rate() -> None:
    """A route decorated with limiter.limit(...) 429s once its limit is exceeded."""
    client = _client(AsyncLimiter("async+memory://", key_func=lambda request: "fixed-test-key"))
    assert client.get("/limited").status_code == 200
    response = client.get("/limited")
    assert response.status_code == 429
    assert response.json() == {"detail": "1 per 1 minute"}


def test_disabled_limiter_does_not_enforce() -> None:
    """With enabled=False the decorated route is never limited."""
    throwaway_limiter = AsyncLimiter("memory://", key_func=lambda request: "k")
    throwaway_limiter.enabled = False
    client = _client(throwaway_limiter)
    assert client.get("/limited").status_code == 200
    assert client.get("/limited").status_code == 200


def test_requests_without_a_client_key_are_not_limited() -> None:
    """An empty key (no client address) skips the check, as slowapi did."""
    client = _client(AsyncLimiter("memory://", key_func=lambda request: ""))
    assert client.get("/limited").status_code == 200
    assert client.get("/limited").status_code == 200


def test_limit_rejects_sync_handlers() -> None:
    """Only coroutine functions can be awaited-checked."""

    def sync_handler(request: Request) -> None: ...

    with pytest.raises(TypeError, match="async"):
        limiter.limit("1/minute")(sync_handler)


@pytest.mark.anyio
async def test_wrapper_requires_a_real_request() -> None:
    """A non-Request `request` argument is a programming error."""
    throwaway_limiter = AsyncLimiter("memory://")

    @throwaway_limiter.limit("1/minute")
    async def handler(request: Request) -> None: ...

    with pytest.raises(TypeError, match="Request"):
        await handler(request="nope")


def test_non_async_storage_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A storage URI that resolves to a sync limits storage fails fast at construction."""
    monkeypatch.setattr("app.rate_limit.storage_from_string", lambda uri, **options: object())
    with pytest.raises(TypeError, match="did not resolve to an async limits storage"):
        AsyncLimiter("async+memory://")
