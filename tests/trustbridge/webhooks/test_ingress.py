"""The webhook route (0028 invariants 1, 2, 25, 28, 29; 0030 invariants 7 and 11)."""

import hmac
import json
from collections.abc import MutableMapping
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from tests.trustbridge.fakes import FakeHostChecker, FakeRedis, FakeSecretStore
from tests.trustbridge.helpers import as_redis, host, make_monitor, make_settings, sign
from trustbridge.github.webhook_secrets import WebhookSecretResolver
from trustbridge.webhooks.ingress import WebhookRuntime, router
from trustbridge.webhooks.limiter import (
    InMemoryWindowCounter,
    ValkeyWindowCounter,
    WebhookLimiter,
    WindowCounter,
)
from trustbridge.worker.deliveries import InMemoryDeliveryStore, NewDelivery
from trustbridge.worker.queue import StreamQueue
from trustbridge.worker.relay import Relay

BODY = json.dumps({"action": "created", "installation": {"id": 7}}).encode()
SECRET = "s3cret"  # noqa: S105 -- test value


class Setup:
    """An app with the webhook router, an in-memory outbox and two hosts."""

    def __init__(self, **settings_kw: Any) -> None:  # noqa: ANN401
        """Initialise."""
        ok_ref, bad_ref = "env:OK", "env:BAD"
        self.ok = host("ok", webhook_secret_ref=ok_ref)
        self.bad = host("bad", webhook_secret_ref=bad_ref)
        self.fresh = host("fresh", app_id=None, webhook_secret_ref=None)
        self.settings = make_settings([self.ok, self.bad, self.fresh], **settings_kw)
        self.store = InMemoryDeliveryStore()
        self.secret_store = FakeSecretStore({"env:OK": SECRET, "env:BAD": "other"})
        self.checker = FakeHostChecker(failing={"bad"})
        self.monitor = make_monitor(self.settings.github_hosts, self.checker)
        self.secrets = WebhookSecretResolver(
            self.monitor,
            self.secret_store,
            read_timeout=1,
            refresh_min_seconds=self.settings.webhook_secret_refresh_min_seconds,
        )
        self.valkey = FakeRedis()
        self.relay = Relay(self.store, StreamQueue(as_redis(self.valkey)))
        self.counter: WindowCounter = InMemoryWindowCounter()
        self.app = FastAPI()
        self.app.include_router(router)

    async def start(self) -> Setup:
        """Start."""
        await self.monitor.check_all()
        self.app.state.trustbridge = WebhookRuntime(
            settings=self.settings,
            monitor=self.monitor,
            secrets=self.secrets,
            store=self.store,
            limiter=WebhookLimiter(
                counter=self.counter,
                enabled=True,
                requests=self.settings.webhook_rate_limit_requests,
                window_seconds=60,
                timeout_ms=50,
            ),
            relay=self.relay,
        )
        return self

    async def post(
        self,
        name: str = "ok",
        body: bytes = BODY,
        *,
        delivery: str | None = "d-1",
        signature: str | None = "auto",
        signed_with: str = SECRET,
    ) -> httpx.Response:
        """Post."""
        headers = {"x-github-event": "installation", "content-type": "application/json"}
        if delivery is not None:
            headers["x-github-delivery"] = delivery
        if signature == "auto":
            headers["x-hub-signature-256"] = sign(signed_with.encode(), body)
        elif signature is not None:
            headers["x-hub-signature-256"] = signature
        transport = httpx.ASGITransport(app=self.app, client=("203.0.113.5", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://tb") as client:
            return await client.post(f"/webhooks/github/{name}", content=body, headers=headers)


@pytest.fixture
async def tb() -> Setup:
    """Tb."""
    return await Setup().start()


async def test_2xx_only_after_the_row_is_committed_and_a_repeat_makes_no_second_row(
    tb: Setup,
) -> None:  # inv. 1
    """2xx only after the row is committed and a repeat makes no second row."""
    seen_at_insert: list[int] = []
    real_insert = tb.store.insert

    async def spying_insert(delivery: NewDelivery) -> bool:
        created = await real_insert(delivery)
        seen_at_insert.append(len(tb.store.rows))  # the row exists before the handler returns
        return created

    tb.store.insert = spying_insert  # type: ignore[method-assign]
    first = await tb.post()
    assert first.status_code == 202
    assert first.json() == {"status": "accepted"}
    assert seen_at_insert == [1]
    row = tb.store.rows["d-1"]
    assert (row.host, row.event, row.body, row.status) == ("ok", "installation", BODY, "pending")
    assert "x-hub-signature-256" not in row.headers  # only allow-listed headers are stored
    again = await tb.post()
    assert again.status_code == 202
    assert again.json() == {"status": "duplicate"}
    assert len(tb.store.rows) == 1


async def test_failed_commit_is_never_a_2xx(tb: Setup) -> None:  # inv. 1

    """Failed commit is never a 2xx."""
    async def boom(delivery: NewDelivery) -> bool:
        raise ConnectionError("database down")

    tb.store.insert = boom  # type: ignore[method-assign]
    assert (await tb.post()).status_code == 503


async def test_relay_adds_only_the_delivery_id_after_the_response(tb: Setup) -> None:
    """Relay adds only the delivery id after the response."""
    await tb.post()
    assert tb.valkey.entries("tb:events")[0][1] == {"id": "d-1"}  # the id, never the body
    assert tb.store.rows["d-1"].last_relayed_at is not None


async def test_valkey_outage_does_not_block_ingest(tb: Setup) -> None:  # 0028 "request path"
    """Valkey outage does not block ingest."""
    tb.valkey.down = True
    tb.app.state.trustbridge.limiter.counter = ValkeyWindowCounter(as_redis(tb.valkey))
    response = await tb.post()
    assert response.status_code == 202  # limiter failed open, relay failed quietly
    assert "d-1" in tb.store.rows
    assert tb.store.rows["d-1"].last_relayed_at is None  # the reconciler relays it later


async def test_signature_is_checked_with_compare_digest_before_anything_is_stored(
    tb: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:  # inv. 2
    """Signature is checked with compare digest before anything is stored."""
    calls: list[int] = []
    real = hmac.compare_digest

    def counting(a: bytes, b: bytes) -> bool:
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr("trustbridge.webhooks.ingress.hmac.compare_digest", counting)
    assert (await tb.post(signed_with="wrong")).status_code == 401
    assert (await tb.post(signature=None)).status_code == 401
    assert (await tb.post(signature="sha1=abc")).status_code == 401
    assert tb.store.rows == {}
    assert calls  # the comparison is the constant-time primitive


async def test_non_json_body_with_a_valid_signature_is_stored_unparsed(tb: Setup) -> None:
    """Non json body with a valid signature is stored unparsed."""
    junk = b"\xff not json"
    assert (await tb.post(body=junk)).status_code == 202
    assert tb.store.rows["d-1"].body == junk


async def test_malformed_delivery_headers_are_400(tb: Setup) -> None:
    """Malformed delivery headers are 400."""
    assert (await tb.post(delivery=None)).status_code == 400
    assert (await tb.post(delivery="has space")).status_code == 400


async def _raw_asgi(
    app: FastAPI,
    path: str,
    headers: list[tuple[bytes, bytes]],
    chunks: list[bytes],
) -> tuple[int, list[int]]:
    """Call the app with a raw ASGI receive that records how many chunks were pulled."""
    pulled: list[int] = []
    queue = list(chunks)

    async def receive() -> MutableMapping[str, Any]:
        pulled.append(1)
        data = queue.pop(0) if queue else b""
        return {"type": "http.request", "body": data, "more_body": bool(queue)}

    status: list[int] = []

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status.append(message["status"])

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("203.0.113.5", 1234),
        "server": ("tb", 80),
        "root_path": "",
    }
    await app(scope, receive, send)
    return status[0], pulled


async def test_content_length_over_the_cap_is_413_before_the_body_is_read() -> None:  # inv. 2
    """Content length over the cap is 413 before the body is read."""
    tb = await Setup(webhook_max_body_bytes=100).start()
    status, pulled = await _raw_asgi(
        tb.app,
        "/webhooks/github/ok",
        [(b"content-length", b"101"), (b"x-github-event", b"push")],
        [b"x" * 101],
    )
    assert status == 413
    assert pulled == []  # not one body chunk was requested


async def test_streamed_body_crossing_the_cap_is_aborted() -> None:  # inv. 2
    """Streamed body crossing the cap is aborted."""
    tb = await Setup(webhook_max_body_bytes=100).start()
    chunks = [b"x" * 60, b"x" * 60, b"x" * 60, b"x" * 60]  # chunked: no Content-Length
    status, pulled = await _raw_asgi(tb.app, "/webhooks/github/ok", [], chunks)
    assert status == 413
    assert len(pulled) == 2  # aborted at the chunk that crossed the cap
    assert tb.store.rows == {}


async def test_invalid_content_length_is_400() -> None:
    """Invalid content length is 400."""
    tb = await Setup().start()
    status, pulled = await _raw_asgi(tb.app, "/webhooks/github/ok", [(b"content-length", b"-5")], [])
    assert (status, pulled) == (400, [])


async def test_over_limit_is_429_with_retry_after_without_reading_the_body() -> None:  # inv. 25
    """Over limit is 429 with retry after without reading the body."""
    tb = await Setup(webhook_rate_limit_requests=2).start()
    for i in range(2):
        assert (await tb.post(delivery=f"d-{i}")).status_code == 202
    status, pulled = await _raw_asgi(
        tb.app, "/webhooks/github/ok", [(b"content-length", b"10")], [b"0123456789"]
    )
    assert status == 429
    assert pulled == []
    over = await tb.post(delivery="d-9")
    assert over.status_code == 429
    assert 1 <= int(over.headers["retry-after"]) <= 60


async def test_invalid_signatures_count_toward_the_limit() -> None:  # inv. 25
    """Invalid signatures count toward the limit."""
    tb = await Setup(webhook_rate_limit_requests=2).start()
    assert (await tb.post(signed_with="wrong")).status_code == 401
    assert (await tb.post(signed_with="wrong")).status_code == 401
    assert (await tb.post(signed_with="wrong")).status_code == 429
    assert (await tb.post()).status_code == 429  # even a valid one now


async def test_unregistered_host_is_503_with_its_body_unread(tb: Setup) -> None:  # inv. 29
    """Unregistered host is 503 with its body unread."""
    status, pulled = await _raw_asgi(
        tb.app, "/webhooks/github/fresh", [(b"content-length", b"3")], [b"abc"]
    )
    assert status == 503
    assert pulled == []
    assert tb.store.rows == {}


async def test_unavailable_host_is_verified_stored_and_answered_2xx(tb: Setup) -> None:  # inv. 29
    """Unavailable host is verified stored and answered 2xx."""
    assert tb.monitor.status("bad").state.value == "unavailable"
    response = await tb.post("bad", signed_with="other")
    assert response.status_code == 202
    assert tb.store.rows["d-1"].host == "bad"
    assert (await tb.post("bad", delivery="d-2", signed_with="wrong")).status_code == 401


async def test_unknown_host_is_404(tb: Setup) -> None:
    """Unknown host is 404."""
    assert (await tb.post("nope")).status_code == 404


async def test_one_unavailable_host_leaves_other_hosts_ingesting(tb: Setup) -> None:  # 0030 inv. 7
    """One unavailable host leaves other hosts ingesting."""
    assert tb.monitor.is_ready("ok")
    assert not tb.monitor.is_ready("bad")
    assert (await tb.post("ok")).status_code == 202


async def test_secret_store_outage_after_a_successful_read_does_not_stop_ingest(
    tb: Setup,
) -> None:  # 0030 inv. 11
    """Secret store outage after a successful read does not stop ingest."""
    assert (await tb.post(delivery="d-1")).status_code == 202  # first use reads and caches
    tb.secret_store.down = True
    assert (await tb.post(delivery="d-2")).status_code == 202
    assert (await tb.post(delivery="d-3", signed_with="wrong")).status_code == 401


async def test_no_cached_secret_while_the_store_is_down_is_503() -> None:  # 0030 inv. 11
    """No cached secret while the store is down is 503."""
    tb = await Setup().start()
    tb.secret_store.down = True
    assert (await tb.post()).status_code == 503
    assert tb.store.rows == {}


async def test_a_rotated_secret_is_picked_up_after_a_mismatch() -> None:
    """A rotated secret is picked up after a mismatch."""
    tb = await Setup(webhook_secret_refresh_min_seconds=0).start()
    assert (await tb.post(delivery="d-1")).status_code == 202
    tb.secret_store.secrets["env:OK"] = "rotated"
    assert (await tb.post(delivery="d-2", signed_with="rotated")).status_code == 202
    assert (await tb.post(delivery="d-3", signed_with=SECRET)).status_code == 401  # the old one is gone
