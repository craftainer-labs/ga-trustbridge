"""The schema gate and its readiness check (0028 invariants 13, 14, 15 and 16)."""

import asyncio
from typing import Any

import pytest
from craftainer_observability.health.registry import HealthRegistry

from trustbridge.worker.schema import (
    REQUIRED_SCHEMA_VERSION,
    SchemaCheck,
    SchemaGate,
    SchemaWaitTimeout,
    bump_schema_compat_sql,
    register_schema_check,
)


class _Result:
    def __init__(self, value: int | None) -> None:
        """Initialise."""
        self.value = value

    def scalar_one_or_none(self) -> int | None:
        """Scalar one or none."""
        return self.value


class _Session:
    def __init__(self, box: dict[str, Any]) -> None:
        """Initialise."""
        self.box = box

    async def __aenter__(self) -> _Session:
        """Implement __aenter__."""
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Implement __aexit__."""
        return None

    async def execute(self, _stmt: object) -> _Result:
        """Execute."""
        if isinstance(self.box["version"], Exception):
            raise self.box["version"]
        return _Result(self.box["version"])


def _gate(version: int | Exception | None, **kw: Any) -> tuple[SchemaGate, dict[str, Any]]:  # noqa: ANN401
    box: dict[str, Any] = {"version": version}
    return SchemaGate(lambda: _Session(box), **kw), box  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("db_version", "current"),
    [(0, False), (None, False), (REQUIRED_SCHEMA_VERSION, True), (REQUIRED_SCHEMA_VERSION + 5, True)],
)
async def test_gate_accepts_an_equal_or_greater_integer_and_rejects_a_lower_one(
    db_version: int | None, current: bool
) -> None:  # inv. 13
    """Gate accepts an equal or greater integer and rejects a lower one."""
    gate, _ = _gate(db_version)
    assert await gate.refresh() is current
    assert gate.current is current


async def test_gate_needs_no_knowledge_of_the_databases_revision_id() -> None:  # inv. 13
    """Gate needs no knowledge of the databases revision id."""
    gate, _ = _gate(REQUIRED_SCHEMA_VERSION + 1)  # rolled-back code against a newer schema
    assert await gate.refresh()


async def test_an_unreadable_table_is_not_current_rather_than_an_error() -> None:
    """An unreadable table is not current rather than an error."""
    gate, _ = _gate(RuntimeError("relation does not exist"))
    assert await gate.refresh() is False


async def test_mock_mode_never_touches_a_database() -> None:  # inv. 15
    """Mock mode never touches a database."""
    def boom() -> Any:  # noqa: ANN401
        raise AssertionError("mock mode must not open a database session")

    gate = SchemaGate(boom, mock=True)  # type: ignore[arg-type]
    assert gate.current
    assert await gate.refresh()
    await gate.wait(timeout=0.01, poll=0.01)
    assert gate.version is None


async def test_waiting_polls_until_the_schema_becomes_current() -> None:  # inv. 14
    """Waiting polls until the schema becomes current."""
    gate, box = _gate(0)
    waiter = asyncio.create_task(gate.wait(timeout=5, poll=0.01))
    await asyncio.sleep(0.05)
    assert not waiter.done()  # still waiting, no failure
    box["version"] = REQUIRED_SCHEMA_VERSION
    await asyncio.wait_for(waiter, timeout=2)
    assert gate.current


async def test_waiting_fails_only_after_the_timeout() -> None:  # inv. 14
    """Waiting fails only after the timeout."""
    gate, _ = _gate(0)
    ticks = iter([0.0, 1.0, 2.0, 99.0, 100.0, 101.0])
    with pytest.raises(SchemaWaitTimeout):
        await gate.wait(timeout=10, poll=0, monotonic=lambda: next(ticks))


async def test_the_api_form_exits_non_zero_only_after_the_wait() -> None:  # inv. 14
    """The api form exits non zero only after the wait."""
    exits: list[int] = []
    gate, box = _gate(0)
    task = asyncio.create_task(
        gate.wait_or_exit(timeout=0.2, poll=0.01, exit_process=exits.append)
    )
    await asyncio.sleep(0.05)
    assert exits == []  # waiting never exits early
    await asyncio.wait_for(task, timeout=2)
    assert exits == [1]
    box["version"] = REQUIRED_SCHEMA_VERSION
    ok_gate, _ = _gate(REQUIRED_SCHEMA_VERSION)
    await ok_gate.wait_or_exit(timeout=1, poll=0.01, exit_process=exits.append)
    assert exits == [1]  # a current schema never exits


async def test_readiness_follows_the_gate_while_nothing_else_changes() -> None:  # inv. 14
    """Readiness follows the gate while nothing else changes."""
    registry = HealthRegistry()
    gate, box = _gate(0)
    assert register_schema_check(registry, gate)
    (not_ready,) = await registry.run_all()
    assert (not_ready.name, not_ready.healthy) == ("schema", False)
    box["version"] = REQUIRED_SCHEMA_VERSION
    await gate.refresh()
    (ready,) = await registry.run_all()
    assert ready.healthy


async def test_the_check_is_registered_once_however_often_the_lifespan_is_entered() -> None:
    """The check is registered once however often the lifespan is entered."""
    registry = HealthRegistry()  # inv. 16
    first, _ = _gate(0)
    second, box = _gate(REQUIRED_SCHEMA_VERSION)
    assert register_schema_check(registry, first) is True
    assert register_schema_check(registry, second) is False  # a second entry: no duplicate
    await second.refresh()
    results = await registry.run_all()
    assert [r.name for r in results] == ["schema"]
    assert results[0].healthy  # the singleton is rebound to the latest gate
    assert box["version"] == REQUIRED_SCHEMA_VERSION


async def test_an_unbound_check_reports_unhealthy() -> None:
    """An unbound check reports unhealthy."""
    assert (await SchemaCheck().check()).healthy is False


def test_every_migration_records_the_integer_with_greatest() -> None:  # inv. 13
    """Every migration records the integer with greatest."""
    sql = bump_schema_compat_sql(3)
    assert "GREATEST(schema_compat.version, 3)" in sql
    assert "ON CONFLICT (id)" in sql
