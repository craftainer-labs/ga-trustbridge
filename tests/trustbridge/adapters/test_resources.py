"""Non-secret resource operations."""

import pytest

from tests.trustbridge.adapters.conftest import World
from trustbridge.adapters.interface import (
    AdapterError,
    ChangeState,
    ProjectSpec,
    RepoSpec,
    resource_name,
)
from trustbridge.adapters.resources import ResourceCore


async def test_project_is_created_once_and_its_state_persisted(secret_world: World) -> None:
    """Prior state in, new state out: the second call finds the project by name."""
    w = secret_world
    res = ResourceCore(w.deps)
    first = await res.ensure_project(w.tenant, "fake", ProjectSpec("p1"))
    second = await res.ensure_project(w.tenant, "fake", ProjectSpec("p1"))
    assert first.change.state is ChangeState.CREATED
    assert second.change.state is ChangeState.UNCHANGED
    assert first.state is not None
    assert first.state.name == resource_name(w.tenant.id, "project", "p1")
    assert len(w.platform.resources) == 1


async def test_repository_needs_its_project_and_delete_forgets_state(secret_world: World) -> None:
    """A repository of an unknown project is refused; delete is idempotent."""
    w = secret_world
    res = ResourceCore(w.deps)
    with pytest.raises(AdapterError):
        await res.ensure_repository(w.tenant, "fake", "p1", RepoSpec("r1"))
    await res.ensure_project(w.tenant, "fake", ProjectSpec("p1"))
    repo = await res.ensure_repository(w.tenant, "fake", "p1", RepoSpec("r1"))
    assert repo.change.state is ChangeState.CREATED
    deleted = await res.delete(w.tenant, "fake", "repository", "p1/r1")
    assert deleted is not None
    assert deleted.change.state is ChangeState.DELETED
    assert await res.delete(w.tenant, "fake", "repository", "p1/r1") is None
