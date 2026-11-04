"""The authorization-denial record and the sink protocol (ADR 0032, ADR 0033).

A dependency-free leaf: `trustbridge.rbac` (the producer) imports it from here, so the audit
package, which the worker also imports, never imports `trustbridge.rbac` (and through it the
FastAPI app). Dependencies point from the higher layer to the lower one.
"""

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True, slots=True)
class Denial:
    """An authorization denial of an authenticated principal, as the audit log needs it.

    `required_role` is the role's string value. `tenant_id` is the requested tenant id when the
    route is tenant-scoped and the path value is a canonical UUID, else None. `source_ip` and
    `request_id` are None when absent or malformed. `suppressed` and `window_seconds` are set
    only on the summary row `ThrottledDenials` writes when a window closes: how many further
    denials of the same subject and reason were counted but not recorded in that window.
    """

    sub: str
    iss: str | None
    reason: Literal["role_missing", "tenant_not_granted"]
    required_role: str
    scope: Literal["instance", "tenant"]
    tenant_id: str | None
    source_ip: str | None
    request_id: str | None
    suppressed: int = 0
    window_seconds: int = 0


class DenialSink(Protocol):
    """Where denials go (`trustbridge.audit.emit.AuditDenials`); the dependency awaits it."""

    async def denied(self, denial: Denial) -> None:
        """Record the denial. A failure propagates: the request ends in an error, not a pass."""
        ...


__all__ = ["Denial", "DenialSink"]
