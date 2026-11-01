"""`POST /webhooks/github/{host}`: the transactional-outbox ingress (ADR 0028 item 1).

Order, with the invariant each step serves:

1. the limiter dependency (25-28), before the body is touched;
2. host lookup: unknown host 404; the host's webhook secret is resolved
   (`WebhookSecretResolver.secret_for`): an `unregistered` host (`HostUnregisteredError`) or
   no cached or readable secret (`WebhookSecretUnavailableError`, ADR 0030 invariant 11) is 503,
   with the body unread (29);
3. `Content-Length` over `WEBHOOK_MAX_BODY_BYTES` is 413 before any body read, and a
   streamed body that crosses the cap is aborted (2);
4. the signature is verified in constant time against that secret before anything is parsed
   or stored (2); a mismatch re-reads the secret once (rotation) before answering 401;
5. the delivery is committed to `webhook_deliveries` (1); only then is 2xx (202) returned. A
   repeated delivery id returns 2xx without a second row. An `unavailable` host goes through
   the same path (29): its rows wait in the outbox.

Valkey is never required here: the limiter fails open and the stream relay runs after the
response, best effort. No GitHub or adapter call happens on the request path, and the JSON
body is never parsed here.
"""

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from trustbridge.adapters.store import ConfigResolver, TenantResolver
from trustbridge.github.availability import HostMonitor
from trustbridge.github.deployments import ArgoCdDeploymentHandler
from trustbridge.github.webhook_secrets import (
    HostUnregisteredError,
    WebhookSecretResolver,
    WebhookSecretUnavailableError,
)
from trustbridge.settings import TrustbridgeSettings
from trustbridge.webhooks.limiter import WebhookLimiter
from trustbridge.worker.deliveries import DeliveryStore, NewDelivery
from trustbridge.worker.relay import Clock, Relay, utcnow

logger = logging.getLogger(__name__)

SIGNATURE_PREFIX = "sha256="
_DELIVERY_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_EVENT = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
STORED_HEADERS = (
    "x-github-event",
    "x-github-delivery",
    "x-github-hook-id",
    "x-github-hook-installation-target-id",
    "x-github-hook-installation-target-type",
    "x-github-enterprise-host",
    "x-github-enterprise-version",
    "user-agent",
    "content-type",
)
router = APIRouter()


@dataclass(slots=True)
class ArgocdRuntime:
    """What the Argo CD notification route needs (`trustbridge.webhooks.argocd`).

    `handler` is None until a `DeploymentStateSource` exists (NG5d); the route then answers 503.
    """

    handler: ArgoCdDeploymentHandler | None
    tenants: TenantResolver
    configs: ConfigResolver


@dataclass(slots=True)
class WebhookRuntime:
    """Everything the routes need; built by the lifespan and kept on `app.state.trustbridge`."""

    settings: TrustbridgeSettings
    monitor: HostMonitor
    secrets: WebhookSecretResolver
    store: DeliveryStore
    limiter: WebhookLimiter
    relay: Relay | None = None
    clock: Clock = utcnow
    argocd: ArgocdRuntime | None = None


def verify_signature(secret: bytes, body: bytes, header: str | None) -> bool:
    """Constant-time check of `X-Hub-Signature-256` (`sha256=<hex>`) over the raw body."""
    if header is None or not header.startswith(SIGNATURE_PREFIX):
        return False
    expected = SIGNATURE_PREFIX + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), header.encode())


def runtime_of(request: Request) -> WebhookRuntime:
    """The runtime installed by the lifespan; absent means the process is not set up."""
    runtime: WebhookRuntime | None = getattr(request.app.state, "trustbridge", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="webhook ingress is not ready")
    return runtime


async def enforce_rate_limit(request: Request) -> None:
    """First step of the route: count the source address, 429 with `Retry-After` when over."""
    runtime = runtime_of(request)
    address = request.client.host if request.client is not None else None
    decision = await runtime.limiter.check(address)
    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail="too many webhook requests from this address",
            headers={"Retry-After": str(decision.retry_after)},
        )


def check_content_length(request: Request, limit: int) -> None:
    """Reject an over-cap or malformed `Content-Length` before any body byte is read."""
    raw = request.headers.get("content-length")
    if raw is None:
        return
    if not raw.isascii() or not raw.isdigit():
        raise HTTPException(status_code=400, detail="invalid Content-Length")
    if int(raw) > limit:
        raise HTTPException(status_code=413, detail="webhook body too large")


async def read_body(request: Request, limit: int) -> bytes:
    """Read the body, aborting as soon as it crosses the cap (chunked bodies included)."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail="webhook body too large")
        chunks.append(chunk)
    return b"".join(chunks)


async def _secret(runtime: WebhookRuntime, host: str) -> bytes:
    """The host's webhook secret, or 503 (unregistered host, or no cached or readable secret)."""
    try:
        return await runtime.secrets.secret_for(host)
    except HostUnregisteredError as exc:
        raise HTTPException(
            status_code=503, detail="GitHub host has no App registered yet"
        ) from exc
    except WebhookSecretUnavailableError as exc:
        raise HTTPException(status_code=503, detail="webhook secret unavailable") from exc


async def _authentic(
    runtime: WebhookRuntime, host: str, secret: bytes, request: Request, body: bytes
) -> None:
    """401 unless the body matches the signature (a rotated secret is tried once)."""
    signature = request.headers.get("x-hub-signature-256")
    if verify_signature(secret, body, signature):
        return
    rotated = await runtime.secrets.refreshed(host)
    if rotated is not None and verify_signature(rotated, body, signature):
        return
    raise HTTPException(status_code=401, detail="invalid webhook signature")


def _metadata(request: Request) -> tuple[str, str, dict[str, str]]:
    """Validated delivery id and event, and the allow-listed headers to store."""
    delivery_id = request.headers.get("x-github-delivery", "")
    event = request.headers.get("x-github-event", "")
    if not _DELIVERY_ID.match(delivery_id) or not _EVENT.match(event):
        raise HTTPException(status_code=400, detail="missing or malformed GitHub delivery headers")
    headers = {
        name: request.headers[name][:512] for name in STORED_HEADERS if name in request.headers
    }
    return delivery_id, event, headers


@router.post("/webhooks/github/{host}", status_code=202, dependencies=[Depends(enforce_rate_limit)])
async def receive_webhook(host: str, request: Request, background: BackgroundTasks) -> JSONResponse:
    """Verify, store and acknowledge one GitHub webhook delivery."""
    runtime = runtime_of(request)
    if host not in runtime.monitor.names():
        raise HTTPException(status_code=404, detail="unknown GitHub host")
    secret = await _secret(runtime, host)
    limit = runtime.settings.webhook_max_body_bytes
    check_content_length(request, limit)
    body = await read_body(request, limit)
    await _authentic(runtime, host, secret, request, body)
    delivery_id, event, headers = _metadata(request)
    try:
        created = await runtime.store.insert(
            NewDelivery(delivery_id, host, event, headers, body, runtime.clock())
        )
    except Exception as exc:
        logger.exception("could not store a webhook delivery")
        raise HTTPException(status_code=503, detail="could not store the delivery") from exc
    if created and runtime.relay is not None:
        background.add_task(runtime.relay.relay_one, delivery_id)
    return JSONResponse({"status": "accepted" if created else "duplicate"}, status_code=202)
