"""`SqlTenantGate.allows` over a scripted session (the Postgres half is in
`test_deletion_db_integration.py`): which `(disabled, own deletion open)` rows admit which
operation."""

from types import TracebackType
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.adapters.store import OperationKind
from trustbridge.tenancy import current_tenant
from trustbridge.tenancy.gate import SqlTenantGate


class _Result:
    def __init__(self, row: tuple[bool, bool] | None) -> None:
        self._row = row

    def first(self) -> tuple[bool, bool] | None:
        return self._row


class _Session:
    def __init__(self, row: tuple[bool, bool] | None, seen: list[Any]) -> None:
        self._row = row
        self._seen = seen

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(
        self, kind: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        return None

    def begin(self) -> _Session:
        return self

    async def execute(self, statement: Any, params: dict[str, Any]) -> _Result:  # noqa: ANN401
        self._seen.append((str(statement), params, current_tenant.get()))
        return _Result(self._row)


def _gate(row: tuple[bool, bool] | None, seen: list[Any]) -> SqlTenantGate:
    return SqlTenantGate(cast(async_sessionmaker[AsyncSession], lambda: _Session(row, seen)))


@pytest.mark.parametrize("kind", [None, *OperationKind])
@pytest.mark.parametrize("row", [None, (False, False), (False, True)])
async def test_an_enabled_or_unknown_tenant_admits_everything(
    row: tuple[bool, bool] | None, kind: OperationKind | None
) -> None:
    """Only a disabled tenant is refused."""
    assert await _gate(row, []).allows(uuid4(), kind) is True


@pytest.mark.parametrize("kind", [None, *OperationKind])
async def test_a_disabled_tenant_without_its_own_open_deletion_refuses_everything(
    kind: OperationKind | None,
) -> None:
    """The admin disable of ADR 0033, unchanged; a stray or terminal deletion id changes nothing."""
    assert await _gate((True, False), []).allows(uuid4(), kind, uuid4()) is False
    assert await _gate((True, False), []).allows(uuid4(), kind) is False


@pytest.mark.parametrize("kind", [None, *OperationKind])
async def test_a_disabled_tenant_admits_only_a_delete_carrying_its_own_open_deletion(
    kind: OperationKind | None,
) -> None:
    """B10: the deletion's own delete and revoke operations, nothing else."""
    allowed = await _gate((True, True), []).allows(uuid4(), kind, uuid4())
    assert allowed is (kind is OperationKind.DELETE)


@pytest.mark.parametrize("kind", [None, *OperationKind])
async def test_a_user_initiated_delete_carries_no_deletion_and_is_refused(
    kind: OperationKind | None,
) -> None:
    """Even if the tenant has an open deletion (the row says so), no id means no admission."""
    assert await _gate((True, True), []).allows(uuid4(), kind) is False


async def test_the_read_is_one_statement_in_no_tenant_context() -> None:
    """`tb_get_tenant` is the unscoped path to `tenants` (ADR 0031)."""
    seen: list[Any] = []
    tenant_id = uuid4()
    deletion = uuid4()
    await _gate((True, True), seen).allows(tenant_id, OperationKind.DELETE, deletion)
    ((sql, params, context),) = seen
    assert "tb_get_tenant" in sql
    assert "tenant_deletions" in sql
    assert params == {"id": tenant_id, "deletion": deletion}
    assert context is None
