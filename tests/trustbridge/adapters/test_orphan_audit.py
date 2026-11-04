"""The orphan sweep tells its observer about each deletion (ADR 0032 credential events)."""

from dataclasses import replace
from uuid import UUID

from tests.trustbridge.adapters.conftest import PULL, World
from trustbridge.adapters.core import CredentialCore
from trustbridge.adapters.interface import credential_name


class Orphans:
    """An `OrphanObserver` that records its calls."""

    def __init__(self) -> None:
        """Start empty."""
        self.calls: list[tuple[UUID, str, str]] = []

    async def orphan_deleted(self, tenant_id: UUID, adapter: str, name: str) -> None:
        """Record the deletion."""
        self.calls.append((tenant_id, adapter, name))


async def test_each_swept_orphan_is_reported_after_the_platform_delete(secret_world: World) -> None:
    """Only a credential nothing accounts for is deleted and reported; accounted ones are not."""
    w = secret_world
    await w.core.ensure_scope(w.tenant, "fake", PULL)
    orphan = credential_name(w.tenant.id, "ab" * 8, 1)
    w.platform.credentials[orphan] = {"id": "x", "kind": "secret", "secret": "s"}
    observer = Orphans()
    core = CredentialCore(replace(w.deps, orphans=observer))
    assert await core.sweep_orphans(w.tenant, "fake") == [orphan]
    assert observer.calls == [(w.tenant.id, "fake", orphan)]
    assert orphan not in w.platform.credentials
