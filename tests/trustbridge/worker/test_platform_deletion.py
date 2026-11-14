"""`SqlPlatformPort.remove` and `ConnectionNames` at unit level (NG6h-5)."""

from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from tests.trustbridge.adapters.conftest import PUSH_PROD, World, build_world
from trustbridge.adapters.core import ScopeResult
from trustbridge.adapters.fake import fake_capabilities
from trustbridge.adapters.interface import ChangeState, ProjectSpec, RepoSpec
from trustbridge.adapters.resources import ResourceCore
from trustbridge.adapters.store import OperationStatus
from trustbridge.deletion.ports import PlatformItem
from trustbridge.worker.platform_deletion import (
    ConnectionNames,
    PlatformDeleteIncompleteError,
    SqlPlatformPort,
)

DELETION = uuid4()


def _port(w: World, core: Any = None) -> SqlPlatformPort:  # noqa: ANN401 -- a stub core
    return SqlPlatformPort(
        cast("Any", None),
        w.store,
        w.deps.tenants,
        core or w.core,
        ResourceCore(w.deps),
    )


async def test_a_credential_is_deleted_without_touching_github() -> None:
    """The platform credential goes, the GitHub outputs stay recorded for the GitHub step."""
    w = build_world(fake_capabilities(oidc=False))
    await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    (stored,) = await w.store.list_credentials(w.tenant.id, "fake")
    await _port(w).remove(
        w.tenant.id, DELETION, PlatformItem("credential", "fake", stored.scope_key)
    )
    assert w.platform.credentials == {}
    assert w.writer.removed == []
    (after,) = await w.store.list_credentials(w.tenant.id, "fake")
    assert after.state.current is None
    assert after.outputs == stored.outputs


async def test_a_credential_row_that_is_gone_is_not_an_error() -> None:
    """Idempotent: a row removed between listing and removing needs nothing."""
    w = build_world(fake_capabilities(oidc=False))
    await _port(w).remove(w.tenant.id, DELETION, PlatformItem("credential", "fake", "nope"))
    assert w.platform.calls == []


class _ManualCore:
    """A core whose delete leaves the operation waiting for a manual action."""

    async def delete_scope(self, *args: object, **kwargs: object) -> ScopeResult:
        """Not finished."""
        return ScopeResult(uuid4(), OperationStatus.MANUAL, ChangeState.NEEDS_MANUAL_ACTION)


async def test_a_delete_that_needs_a_manual_action_is_an_error_the_step_retries() -> None:
    """NEEDS_MANUAL_ACTION is transient: the port raises, the engine retries and alerts."""
    w = build_world(fake_capabilities(oidc=False))
    await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    (stored,) = await w.store.list_credentials(w.tenant.id, "fake")
    item = PlatformItem("credential", "fake", stored.scope_key)
    with pytest.raises(PlatformDeleteIncompleteError):
        await _port(w, _ManualCore()).remove(w.tenant.id, DELETION, item)


async def test_the_deletion_origin_and_output_flag_reach_the_core() -> None:
    """The core is called with the deletion id and `remove_outputs=False`."""
    w = build_world(fake_capabilities(oidc=False))
    await w.core.ensure_scope(w.tenant, "fake", PUSH_PROD)
    (stored,) = await w.store.list_credentials(w.tenant.id, "fake")
    seen: dict[str, object] = {}

    class Spy:
        async def delete_scope(self, *args: object, **kwargs: object) -> ScopeResult:
            seen.update(kwargs)
            return ScopeResult(None, None, ChangeState.UNCHANGED)

    await _port(w, Spy()).remove(
        w.tenant.id, DELETION, PlatformItem("credential", "fake", stored.scope_key)
    )
    assert seen == {"deletion_id": DELETION, "remove_outputs": False}


async def test_resources_are_removed_through_the_resource_core() -> None:
    """A project, repository or webhook row is removed by `ResourceCore.delete`, then forgotten."""
    w = build_world(fake_capabilities(oidc=False))
    res = ResourceCore(w.deps)
    await res.ensure_project(w.tenant, "fake", ProjectSpec("p1"))
    await res.ensure_repository(w.tenant, "fake", "p1", RepoSpec("r1"))
    port = _port(w)
    await port.remove(w.tenant.id, DELETION, PlatformItem("repository", "fake", "p1/r1"))
    await port.remove(w.tenant.id, DELETION, PlatformItem("project", "fake", "p1"))
    assert await w.store.get_resource(w.tenant.id, "fake", "project:p1") is None
    assert await w.store.get_resource(w.tenant.id, "fake", "repository:p1/r1") is None
    assert w.platform.resources == {}


class _Refs:
    def __init__(self, refs: list[str]) -> None:
        self.refs = refs
        self.prefixes: list[str] = []

    async def list_refs(self, prefix: str) -> list[str]:
        self.prefixes.append(prefix)
        return self.refs


async def test_connection_names_are_adapter_names_only() -> None:
    """Direct children give their adapter name; anything odd is reported as `other`."""
    tenant = UUID(int=9)
    prefix = f"tenants/{tenant}/"
    refs = _Refs(
        [
            f"store:{prefix}argocd",
            f"store:{prefix}harbor",
            f"store:{prefix}nested/value",
            f"store:{prefix}UPPER",
            f"store:{prefix}" + "x" * 65,
            f"store:{prefix}",
        ]
    )
    names = await ConnectionNames(refs).names(tenant)
    assert names == ["argocd", "harbor", "other"]
    assert refs.prefixes == [prefix]
