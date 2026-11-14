"""`SqlWritersPort.quiesce` at unit level (NG6h-7a); the SQL reads are in the integration test."""

from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from trustbridge.adapters.core import OperationFailedError, RetryableOperationError
from trustbridge.worker.drain_deletion import SqlWritersPort
from trustbridge.worker.lease import LeaseBusy


class _Core:
    """A credential core whose resume settles an operation or fails as scripted."""

    def __init__(self, open_ops: list[UUID], fail: dict[UUID, Exception]) -> None:
        self.open_ops = open_ops
        self.fail = fail
        self.resumed: list[UUID] = []

    async def resume_operation(self, operation_id: UUID) -> None:
        self.resumed.append(operation_id)
        if operation_id in self.fail:
            raise self.fail[operation_id]
        self.open_ops.remove(operation_id)


class _Port(SqlWritersPort):
    """The port with its two row reads replaced."""

    def __init__(self, core: _Core, leases: int) -> None:
        super().__init__(cast("Any", None), cast("Any", core))
        self._core = core
        self._leases = leases

    async def _open_operations(self, tenant_id: UUID) -> list[UUID]:
        return list(self._core.open_ops)

    async def _held_leases(self, tenant_id: UUID) -> int:
        return self._leases


async def test_a_tenant_with_nothing_open_is_quiet() -> None:
    """No open operation and no held lease: zero active writers."""
    core = _Core([], {})
    assert await _Port(core, 0).quiesce(uuid4()) == 0
    assert core.resumed == []


async def test_open_operations_are_resumed_under_the_normal_rules() -> None:
    """An operation the core settles no longer counts; a held lease does."""
    ops = [uuid4(), uuid4()]
    core = _Core(list(ops), {})
    assert await _Port(core, 1).quiesce(uuid4()) == 1
    assert core.resumed == ops


@pytest.mark.parametrize(
    "error",
    [RetryableOperationError(uuid4(), "x"), OperationFailedError(uuid4(), "x"), LeaseBusy()],
)
async def test_an_operation_that_cannot_settle_stays_active_without_stopping_the_pass(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    """A platform, lease or operation failure is logged by class and counted as active."""
    stuck, fine = uuid4(), uuid4()
    core = _Core([stuck, fine], {stuck: error})
    assert await _Port(core, 0).quiesce(uuid4()) == 1
    assert core.resumed == [stuck, fine]
    assert type(error).__name__ in caplog.text
