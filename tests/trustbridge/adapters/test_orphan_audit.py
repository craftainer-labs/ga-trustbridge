"""The orphan sweep tells its observer about each deletion (ADR 0032 credential events)."""

from dataclasses import replace
from uuid import UUID

import pytest

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


class FailingOrphans(Orphans):
    """An observer whose audit write fails."""

    async def orphan_deleted(self, tenant_id: UUID, adapter: str, name: str) -> None:
        """Record the call, then fail."""
        await super().orphan_deleted(tenant_id, adapter, name)
        raise RuntimeError("audit sink down")


async def test_a_failing_audit_write_does_not_skip_the_remaining_orphans(
    secret_world: World,
) -> None:
    """Every orphan is deleted and offered to the observer; the sweep then fails."""
    w = secret_world
    await w.core.ensure_scope(w.tenant, "fake", PULL)
    names = [credential_name(w.tenant.id, "ab" * 8, n) for n in (1, 2)]
    for name in names:
        w.platform.credentials[name] = {"id": "x", "kind": "secret", "secret": "s"}
    observer = FailingOrphans()
    core = CredentialCore(replace(w.deps, orphans=observer))
    with pytest.raises(RuntimeError, match="audit sink down"):
        await core.sweep_orphans(w.tenant, "fake")
    assert sorted(c[2] for c in observer.calls) == sorted(names)
    assert not set(names) & set(w.platform.credentials)


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
