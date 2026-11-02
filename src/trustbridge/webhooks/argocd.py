"""`POST /webhooks/argocd/{tenant_id}`: Argo CD notifications (ADR 0029 `STATIC_HEADER`).

Argo CD's notifications webhook service sends a fixed `Authorization: Bearer <token>` header.
The token is the tenant's `notification_token` in its `argocd` connection data and is compared
in constant time (`verify_static_header`). The body is never trusted: it only names the
application, and `ArgoCdDeploymentHandler.handle` re-reads the rollout state through the
`DeploymentStateSource` (NG5d) before a GitHub deployment status is written. Unknown tenant,
missing token and wrong token are all 401, so a probe learns nothing. Without a state source
the route answers 503; a host that is not `ready` also answers 503 (Argo CD retries).
"""

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from trustbridge.github.availability import HostNotReadyError
from trustbridge.github.deployments import verify_static_header
from trustbridge.webhooks.ingress import (
    check_content_length,
    enforce_rate_limit,
    read_body,
    runtime_of,
)

ADAPTER = "argocd"
TOKEN_KEY = "notification_token"  # noqa: S105 -- the name of a connection-data key
BEARER = "Bearer "
router = APIRouter()


def bearer_token(request: Request) -> str | None:
    """The token of an `Authorization: Bearer` header, if the request has one."""
    header = request.headers.get("authorization", "")
    return header.removeprefix(BEARER) if header.startswith(BEARER) else None


@router.post(
    "/webhooks/argocd/{tenant_id}", status_code=202, dependencies=[Depends(enforce_rate_limit)]
)
async def argocd_notification(tenant_id: UUID, request: Request) -> JSONResponse:
    """Authenticate the tenant's notification, then report the re-read rollout state."""
    runtime = runtime_of(request)
    argocd = runtime.argocd
    if argocd is None or argocd.handler is None:
        raise HTTPException(status_code=503, detail="Argo CD reporting is not enabled")
    try:
        tenant = await argocd.tenants.get_tenant(tenant_id)
    except LookupError as exc:
        raise HTTPException(status_code=401, detail="invalid notification token") from exc
    config = await argocd.configs.config_for(tenant, ADAPTER)
    expected = config.connection.get(TOKEN_KEY)
    if expected is None or not verify_static_header(
        expected.get_secret_value(), bearer_token(request)
    ):
        raise HTTPException(status_code=401, detail="invalid notification token")
    limit = runtime.settings.webhook_max_body_bytes
    check_content_length(request, limit)
    try:
        payload: Any = json.loads(await read_body(request, limit))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="body is not JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body is not a JSON object")
    try:
        deployment_id = await argocd.handler.handle(tenant, payload)
    except HostNotReadyError as exc:
        raise HTTPException(status_code=503, detail="GitHub host is not ready") from exc
    return JSONResponse({"deployment_id": deployment_id}, status_code=202)
