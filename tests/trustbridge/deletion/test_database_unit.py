"""`SqlDatabaseStep` over a scripted session: the batches, the cursor, the lease guard, the
category names and the tenant context (the Postgres half is in `test_database_integration.py`)."""

from types import TracebackType
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.trustbridge.deletion.fakes import TENANT, record
from trustbridge.audit.redact import deletion_counts, has_secret_word
from trustbridge.deletion.database import PLAN, SqlDatabaseStep
from trustbridge.deletion.model import Lease, LeaseLost, StepContext
from trustbridge.tenancy import current_tenant

REC = record(state="database", host="gh.example")
LEASE = Lease(REC.id, "h-1", 7)


class _Result:
    def __init__(self, scalar: int) -> None:
        self._scalar = scalar

    def first(self) -> int | None:
        return 1 if self._scalar else None

    def scalar_one(self) -> int:
        return self._scalar


class _Log:
    def __init__(self, *, held: bool = True, removed: int = 0, unresolved: int = 0) -> None:
        self.held, self.removed, self.unresolved = held, removed, unresolved
        self.statements: list[tuple[str, dict[str, Any], Any]] = []
        self.committed = 0
        self.rolled_back = 0


class _Tx:
    def __init__(self, log: _Log) -> None:
        self.log = log

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self, kind: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if exc is None:
            self.log.committed += 1
        else:
            self.log.rolled_back += 1


class _Session:
    def __init__(self, log: _Log) -> None:
        self.log = log

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(
        self, kind: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        return None

    def begin(self) -> _Tx:
        return _Tx(self.log)

    async def execute(self, statement: Any, params: dict[str, Any]) -> _Result:  # noqa: ANN401
        sql = str(statement)
        self.log.statements.append((sql, params, current_tenant.get()))
        if "FROM tenant_deletions" in sql:
            return _Result(1 if self.log.held else 0)
        if "tenant_id IS NULL" in sql:
            return _Result(self.log.unresolved)
        return _Result(self.log.removed)


def _step(log: _Log, batch_size: int = 3) -> SqlDatabaseStep:
    factory = cast(async_sessionmaker[AsyncSession], lambda: _Session(log))
    return SqlDatabaseStep(factory, batch_size=batch_size)


def _ctx(cursor: dict[str, Any] | None = None) -> StepContext:
    async def renew() -> None:
        return None

    return StepContext(
        record(state="database", host="gh.example", step_cursor=cursor or {}), LEASE, renew
    )


def test_category_names_avoid_the_admin_views_secret_words_and_pass_the_audit_check() -> None:
    """A secret word would drop the count from the audit view (follow-up a)."""
    categories = [category for _, category in PLAN]
    assert not any(has_secret_word(name) for name in categories)
    assert deletion_counts(dict.fromkeys(categories, 1))


async def test_a_full_batch_asks_for_another_pass_on_the_same_table() -> None:
    """A batch that filled the limit saves the table and the running total."""
    log = _Log(removed=3)
    outcome = await _step(log).run(_ctx({"table": "adapter_credentials", "removed": 3}))
    assert outcome.done is False
    assert outcome.counts == {"access_grants": 3}
    assert outcome.cursor == {"table": "adapter_credentials", "removed": 6}


async def test_a_short_batch_moves_to_the_next_table() -> None:
    """Fewer rows than the limit means the table is clear: the cursor points at the next one."""
    log = _Log(removed=2)
    outcome = await _step(log).run(_ctx())
    assert outcome.done is False
    assert outcome.counts == {"adapter_operations": 2}
    assert outcome.cursor == {"table": "adapter_credentials", "removed": 0}


async def test_an_empty_table_reports_no_count() -> None:
    """Nothing removed adds nothing to the counts."""
    outcome = await _step(_Log()).run(_ctx({"table": "scope_leases"}))
    assert outcome.counts == {}
    assert outcome.cursor == {"table": "tenant_repositories", "removed": 0}


async def test_the_last_table_finishes_and_lists_the_unresolved_deliveries() -> None:
    """Bodies cleared, then one `retention_only` skip for deliveries with no tenant."""
    log = _Log(removed=1, unresolved=4)
    outcome = await _step(log).run(_ctx({"table": "webhook_deliveries", "removed": 0}))
    assert outcome.done is True
    assert outcome.counts == {"webhook_bodies": 1}
    assert outcome.skipped == [
        {"category": "webhook_bodies_unresolved", "reason": "retention_only", "count": 4}
    ]
    unresolved = [s for s in log.statements if "tenant_id IS NULL" in s[0]]
    assert unresolved[0][1] == {"host": "gh.example"}


async def test_the_last_table_with_nothing_unresolved_skips_nothing() -> None:
    """No unattributable deliveries, no skipped entry."""
    outcome = await _step(_Log()).run(_ctx({"table": "webhook_deliveries"}))
    assert outcome.done is True
    assert outcome.skipped == []


async def test_a_full_last_batch_does_not_count_the_unresolved_yet() -> None:
    """More bodies may remain: another pass, and the unresolved count waits for the final one."""
    log = _Log(removed=3)
    outcome = await _step(log).run(_ctx({"table": "webhook_deliveries"}))
    assert outcome.done is False
    assert not any("tenant_id IS NULL" in s[0] for s in log.statements)


@pytest.mark.parametrize(
    "cursor",
    [{}, {"table": "nope"}, {"table": 7}, {"removed": 5}],
)
async def test_an_unreadable_cursor_starts_over_from_the_first_table(
    cursor: dict[str, Any],
) -> None:
    """Every pass deletes what is left, so starting over is safe."""
    log = _Log(removed=3)
    outcome = await _step(log).run(_ctx(cursor))
    assert outcome.cursor == {"table": "credential_operations", "removed": 3}


@pytest.mark.parametrize("removed", [-1, "x", True])
async def test_an_unreadable_running_total_restarts_at_zero(removed: Any) -> None:  # noqa: ANN401
    """A bad total in the cursor counts as zero (a bool is accepted as the int it is)."""
    log = _Log(removed=3)
    outcome = await _step(log).run(_ctx({"table": "scope_leases", "removed": removed}))
    assert outcome.cursor is not None
    assert outcome.cursor["removed"] == (4 if removed is True else 3)


async def test_each_pass_checks_the_lease_first_and_runs_in_the_tenants_context() -> None:
    """Guard, then one delete, in one transaction inside `tenant_context(tenant_id)`."""
    log = _Log(removed=1)
    await _step(log).run(_ctx())
    guard, delete = log.statements
    assert guard[1] == {"id": REC.id, "holder": "h-1", "fence": 7}
    assert "FROM tenant_deletions" in guard[0]
    assert "DELETE FROM credential_operations" in delete[0]
    assert delete[1] == {"tenant": TENANT, "limit": 3}
    assert guard[2] == delete[2] == TENANT
    assert log.committed == 1
    assert current_tenant.get() is None


async def test_the_webhook_pass_clears_bodies_and_marks_the_rows_deleted() -> None:
    """Never a row delete: the dedupe key stays (ADR 0034 step 5)."""
    log = _Log()
    await _step(log).run(_ctx({"table": "webhook_deliveries"}))
    sql = log.statements[1][0]
    assert "SET body = NULL, status = 'deleted'" in sql
    assert "DELETE" not in sql


async def test_a_lost_lease_stops_before_any_row_is_touched() -> None:
    """No live lease for this holder and fence: `LeaseLost`, nothing deleted, rolled back."""
    log = _Log(held=False, removed=5)
    with pytest.raises(LeaseLost):
        await _step(log).run(_ctx())
    assert len(log.statements) == 1
    assert log.rolled_back == 1


def test_a_batch_size_below_one_is_refused() -> None:
    """A zero batch would never finish."""
    with pytest.raises(ValueError, match="batch_size"):
        _step(_Log(), batch_size=0)
