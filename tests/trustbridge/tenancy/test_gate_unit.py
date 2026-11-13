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


class _OneResult:
    def __init__(self, row: tuple[bool, bool, bool, bool]) -> None:
        self._row = row

    def one(self) -> tuple[bool, bool, bool, bool]:
        return self._row


class _OneSession(_Session):
    def __init__(self, row: tuple[bool, bool, bool, bool], seen: list[Any]) -> None:
        super().__init__(None, seen)
        self._one = row

    async def execute(self, statement: Any, params: dict[str, Any]) -> _OneResult:  # type: ignore[override]  # noqa: ANN401
        self._seen.append((str(statement), params, current_tenant.get()))
        return _OneResult(self._one)


@pytest.mark.parametrize(
    ("row", "refusal"),
    [
        ((True, False, False, False), "gone"),  # no tenant row
        ((False, True, False, False), "gone"),  # installation state `deleted`
        ((False, False, False, True), "deleting"),
        ((False, False, True, False), "disabled"),
        ((False, False, True, True), "deleting"),  # a deletion outranks the disable
        ((False, False, False, False), None),
    ],
)
async def test_refusal_of_names_why_a_reconcile_may_not_run(
    row: tuple[bool, bool, bool, bool], refusal: str | None
) -> None:
    """Gone, deleting, disabled in that order; read in no tenant context."""
    seen: list[Any] = []
    gate = SqlTenantGate(cast(async_sessionmaker[AsyncSession], lambda: _OneSession(row, seen)))
    tenant = uuid4()
    assert await gate.refusal_of(tenant) == refusal
    assert [(p, t) for _, p, t in seen] == [({"id": tenant}, None)]
