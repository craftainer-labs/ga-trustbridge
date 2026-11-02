"""Discovery and validation of adapters through the `trustbridge.adapters` entry-point group."""

from collections.abc import Callable, Iterable
from importlib.metadata import EntryPoint, entry_points
from typing import cast

from trustbridge.adapters.interface import (
    API_VERSION,
    MIN_SUPPORTED_API_VERSION,
    AdapterConfig,
    Capabilities,
    PlatformAdapter,
)

ENTRY_POINT_GROUP = "trustbridge.adapters"


class AdapterLoadError(Exception):
    """An enabled adapter cannot be loaded or violates the interface."""


def check_api_version(adapter_api_version: int) -> None:
    """Accept an adapter whose `api_version` lies in the supported range.

    Rule (settles ADR 0029's open issue): `api_version` is the highest interface version the
    adapter was written against. Additive changes are non-breaking, so an adapter written
    for an older version keeps working; the core accepts every version from
    `MIN_SUPPORTED_API_VERSION` up to its own `API_VERSION` and refuses a newer one.
    """
    if not MIN_SUPPORTED_API_VERSION <= adapter_api_version <= API_VERSION:
        msg = (
            f"adapter api_version {adapter_api_version} is outside the supported range "
            f"{MIN_SUPPORTED_API_VERSION}..{API_VERSION}"
        )
        raise AdapterLoadError(msg)


def validate_capabilities(caps: Capabilities) -> None:
    """Enforce invariant A7: `oidc_hosts` is empty without OIDC and a subset of `github_hosts`."""
    if not caps.oidc and caps.oidc_hosts:
        msg = "oidc_hosts must be empty when oidc is False"
        raise AdapterLoadError(msg)
    if not caps.oidc_hosts <= caps.github_hosts:
        msg = "oidc_hosts must be a subset of github_hosts"
        raise AdapterLoadError(msg)
    if caps.credential_limit is not None and caps.credential_limit < 1:
        msg = "credential_limit must be positive"
        raise AdapterLoadError(msg)


def load_adapter_classes(
    enabled: Iterable[str],
    *,
    eps: Iterable[EntryPoint] | None = None,
) -> dict[str, type[PlatformAdapter]]:
    """Load only the enabled adapters (`ADAPTERS_ENABLED`); an adapter not listed is never imported.

    An enabled name with no entry point, a failing import, a name mismatch or an
    `api_version` outside the supported range raises `AdapterLoadError`.
    """
    available = {
        ep.name: ep for ep in (entry_points(group=ENTRY_POINT_GROUP) if eps is None else eps)
    }
    loaded: dict[str, type[PlatformAdapter]] = {}
    for name in enabled:
        ep = available.get(name)
        if ep is None:
            msg = f"enabled adapter {name!r} has no entry point in {ENTRY_POINT_GROUP}"
            raise AdapterLoadError(msg)
        try:
            cls = ep.load()
        except Exception as exc:
            msg = f"adapter {name!r} failed to import: {type(exc).__name__}"
            raise AdapterLoadError(msg) from exc
        if getattr(cls, "name", None) != name:
            msg = f"adapter {name!r}: class name attribute {getattr(cls, 'name', None)!r} differs"
            raise AdapterLoadError(msg)
        check_api_version(getattr(cls, "api_version", -1))
        loaded[name] = cast("type[PlatformAdapter]", cls)
    return loaded


AdapterFactory = Callable[[AdapterConfig], PlatformAdapter]


def instantiate(cls: type[PlatformAdapter], config: AdapterConfig) -> PlatformAdapter:
    """Instantiate an adapter for one tenant (`cls(config)`) and validate its capabilities."""
    factory = cast("AdapterFactory", cls)
    adapter: object = factory(config)
    if not isinstance(adapter, PlatformAdapter):
        msg = f"adapter {cls.name!r} does not implement PlatformAdapter"
        raise AdapterLoadError(msg)
    validate_capabilities(adapter.capabilities())
    return adapter
