"""The tenant context, the hook's logic and the iteration helpers, without a database.

ADR 0031 invariants 4, 5 and 11 (the Postgres-backed halves are in `test_rls_integration.py`).
"""

import asyncio
from contextlib import aclosing
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from trustbridge.tenancy import (
    TENANT_SETTING,
    TenantInfo,
    current_tenant,
    for_each_tenant,
    install_tenant_hook,
    iterate_tenants,
    tenant_context,
)
from trustbridge.tenancy import hook as hook_module
from trustbridge.tenancy import session as session_module

SRC = Path(__file__).resolve().parents[3] / "src" / "trustbridge"


def _info(tenant_id: UUID) -> TenantInfo:
    return TenantInfo(tenant_id, "github.com", 1, "org", 1, "active", None, False)


class _Conn:
    """A stand-in connection that records what the hook executes."""

    def __init__(self, dialect: str) -> None:
        self.dialect = SimpleNamespace(name=dialect)
        self.calls: list[dict[str, str]] = []

    def execute(self, _statement: object, params: dict[str, str]) -> None:
        self.calls.append(params)


def test_the_context_is_set_inside_and_reset_after_including_when_the_body_raises() -> None:
    """`tenant_context` resets the ContextVar on exit, also when the body raises (inv. 5)."""
    a, b = uuid4(), uuid4()
    assert current_tenant.get() is None
    with tenant_context(a):
        assert current_tenant.get() == a
        with tenant_context(b):
            assert current_tenant.get() == b
        assert current_tenant.get() == a
        with pytest.raises(RuntimeError), tenant_context(b):
            raise RuntimeError("boom")
        assert current_tenant.get() == a
    assert current_tenant.get() is None


async def test_concurrent_tasks_and_thread_hops_never_see_each_others_context() -> None:  # inv. 4
    """Two tasks with different tenants keep their own across awaits and `to_thread` hops."""
    seen: dict[str, list[UUID | None]] = {}

    async def work(name: str, tenant_id: UUID) -> None:
        with tenant_context(tenant_id):
            observed = [current_tenant.get()]
            await asyncio.sleep(0.01)
            observed.append(current_tenant.get())
            observed.append(await asyncio.to_thread(current_tenant.get))
            await asyncio.sleep(0.01)
            observed.append(current_tenant.get())
        seen[name] = observed

    a, b = uuid4(), uuid4()
    await asyncio.gather(work("a", a), work("b", b))
    assert seen == {"a": [a] * 4, "b": [b] * 4}
    assert current_tenant.get() is None


def test_the_hook_sets_the_uuid_or_an_empty_string_and_only_on_postgres() -> None:  # inv. 5
    """The hook writes the tenant's text, '' when unset, and nothing on another dialect."""
    tenant_id = uuid4()
    pg = _Conn("postgresql")
    hook_module._set_tenant_setting(None, None, pg)  # type: ignore[arg-type]
    with tenant_context(tenant_id):
        hook_module._set_tenant_setting(None, None, pg)  # type: ignore[arg-type]
    assert pg.calls == [{"tenant_id": ""}, {"tenant_id": str(tenant_id)}]
    other = _Conn("sqlite")
    hook_module._set_tenant_setting(None, None, other)  # type: ignore[arg-type]
    assert other.calls == []


def test_installing_the_hook_twice_registers_it_once() -> None:
    """`install_tenant_hook` is idempotent."""
    install_tenant_hook()
    install_tenant_hook()
    assert event.contains(Session, "after_begin", hook_module._set_tenant_setting)


def test_only_the_hook_writes_the_setting_and_only_the_context_manager_the_variable() -> None:
    """No other module sets `app.tenant_id` or `current_tenant` (inv. 5)."""
    setting_writers: set[str] = set()
    var_writers: set[str] = set()
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        rel = path.relative_to(SRC).as_posix()
        if "set_config(" in text and "app.tenant_id" in text:
            setting_writers.add(rel)
        if "current_tenant.set(" in text:
            var_writers.add(rel)
    assert TENANT_SETTING == "app.tenant_id"
    assert setting_writers == {"tenancy/hook.py"}
    assert var_writers == {"tenancy/context.py"}


def test_the_relay_and_the_delivery_reconciler_set_no_tenant_context() -> None:  # inv. 8
    """Host-scoped duties never touch the tenant context (nor iterate tenants)."""
    for name in ("relay.py", "reconciler.py", "deliveries.py"):
        text = (SRC / "worker" / name).read_text()
        assert "tenant_context" not in text
        assert "iterate_tenants" not in text


async def test_iterate_tenants_yields_each_tenant_inside_its_own_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # inv. 11
    """Each tenant once, under its own context only; none is left set after the loop."""
    ids = [uuid4(), uuid4(), uuid4()]

    async def fake_list(_factory: object) -> list[TenantInfo]:
        return [_info(i) for i in ids]

    monkeypatch.setattr(session_module, "list_tenants", fake_list)
    seen: list[tuple[UUID, UUID | None]] = []
    async with aclosing(iterate_tenants(None)) as tenants:  # type: ignore[arg-type]
        async for tenant_id in tenants:
            seen.append((tenant_id, current_tenant.get()))
    assert seen == [(i, i) for i in ids]
    assert current_tenant.get() is None


async def test_one_tenants_failure_does_not_stop_the_others_or_leak_its_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # inv. 11
    """`for_each_tenant` logs the failure, goes on, and the next tenant has only its own."""
    ids = [uuid4(), uuid4(), uuid4()]

    async def fake_list(_factory: object) -> list[TenantInfo]:
        return [_info(i) for i in ids]

    monkeypatch.setattr(session_module, "list_tenants", fake_list)
    contexts: list[UUID | None] = []

    async def work(tenant_id: UUID) -> None:
        contexts.append(current_tenant.get())
        if tenant_id == ids[1]:
            raise RuntimeError("tenant failed")

    result = await for_each_tenant(None, work)  # type: ignore[arg-type]
    assert contexts == ids
    assert result.succeeded == 2
    assert result.failed == (ids[1],)
    assert current_tenant.get() is None
