"""The adapters endpoint's data: which adapters are enabled, which a tenant has data for, and what
each can do (ADR 0033 invariant 8, ADR 0029).

Nothing here reads a connection value. "Configured" is whether the secret store lists a
`store:tenants/<uuid>/<adapter>` entry (existence only, through `list_refs`), and the
capabilities are what the adapter class describes about itself, which for every adapter is
static (it does not depend on the tenant's connection data).
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, cast
from uuid import UUID

from trustbridge.adapters.interface import Capabilities, PlatformAdapter
from trustbridge.adapters.loader import validate_capabilities
from trustbridge.admin.schemas import AdapterOut, CapabilitiesOut, WebhookCapabilityOut

logger = logging.getLogger(__name__)


class SecretRefs(Protocol):
    """The secret store's listing: reference names only, never a value."""

    async def list_refs(self, prefix: str) -> list[str]:
        """The `store:` references whose name starts with `prefix`."""
        ...


def describe(cls: type[PlatformAdapter]) -> Capabilities:
    """The capabilities an adapter class declares, validated (ADR 0029 invariant A7).

    `capabilities()` returns a constant for every adapter, so it is called without an instance
    (an instance needs a tenant's connection data, which this endpoint never reads, and some
    constructors refuse an empty one).
    """
    describe_self = cast("Callable[[object], Capabilities]", cls.capabilities)
    caps = describe_self(None)
    validate_capabilities(caps)
    return caps


def capabilities_out(caps: Capabilities) -> CapabilitiesOut:
    """The response model of `caps`."""
    webhook = caps.webhook
    return CapabilitiesOut(
        oidc=caps.oidc,
        oidc_hosts=sorted(caps.oidc_hosts),
        oidc_requires_manual_step=caps.oidc_requires_manual_step,
        ci_credential=caps.ci_credential,
        rotation=caps.rotation,
        webhook=None
        if webhook is None
        else WebhookCapabilityOut(
            auth=webhook.auth.value, header=webhook.header, trigger_only=webhook.trigger_only
        ),
        github_hosts=sorted(caps.github_hosts),
        needs_privileged_credential=caps.needs_privileged_credential,
        credential_limit=caps.credential_limit,
    )


@dataclass(frozen=True, slots=True)
class AdapterCatalog:
    """The enabled adapter classes and the secret store listing (`app.state.admin.adapters`)."""

    classes: Mapping[str, type[PlatformAdapter]]
    refs: SecretRefs

    async def for_tenant(self, tenant_id: UUID) -> list[AdapterOut]:
        """Every enabled adapter by name, with whether the tenant has connection data for it.

        Raises whatever the secret store raises (it is unavailable: the route answers 503).
        """
        prefix = f"tenants/{tenant_id}/"
        present = set(await self.refs.list_refs(prefix))
        return [
            self._out(name, f"store:{prefix}{name}" in present) for name in sorted(self.classes)
        ]

    def _out(self, name: str, configured: bool) -> AdapterOut:
        try:
            caps: CapabilitiesOut | None = capabilities_out(describe(self.classes[name]))
        except Exception:
            logger.exception("adapter %s cannot describe its capabilities", name)
            caps = None
        return AdapterOut(name=name, configured=configured, capabilities=caps)
