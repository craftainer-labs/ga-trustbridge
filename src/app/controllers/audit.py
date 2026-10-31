"""HTTP routes for /audit -- a minimal example of app.oidc.require_roles."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.config import get_settings
from app.oidc import require_roles

router = APIRouter(tags=["audit"])
settings = get_settings()


@router.get("")
async def audit(
    claims: Annotated[dict[str, Any], Depends(require_roles("security", "detective"))],
) -> dict[str, Any]:
    """Example RBAC-protected route: return the caller's subject and granted roles."""
    return {
        "sub": claims["sub"],
        "roles": claims.get("resource_access", {})
        .get(settings.oidc_client_id, {})
        .get("roles", []),
    }
