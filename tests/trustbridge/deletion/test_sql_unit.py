"""`SqlDeletionStore` over a scripted session: the SQL it sends, the transaction boundaries and
the mapping of `TB003` and `TB004` to `LeaseLost` (the Postgres half is in
`test_sql_integration.py`)."""

import json
from types import TracebackType
from typing import Any, cast

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.trustbridge.deletion.fakes import TENANT, record
from trustbridge.deletion import audit
from trustbridge.deletion.model import Lease, LeaseLost
from trustbridge.deletion.sql import SqlDeletionStore
from trustbridge.tenancy import current_tenant

LEASE = Lease(record().id, "h-1", 7)


class _Result:
    rowcount = 1  # an audit `append` inserted its row (NG6h-2b reads the count)

    def __init__(self, rows: list[dict[str, Any]], scalar: Any = None) -> None:  # noqa: ANN401
        self._rows = rows
        self._scalar = scalar

    def mappings(self) -> _Result:
        return self

    def first(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def scalar_one(self) -> Any:  # noqa: ANN401
        return self._scalar


class _Session:
    """Records statements and the tenant context each ran in; one shared log per factory."""

    def __init__(self, log: _Log) -> None:
        self.log = log

    async def __aenter__(self) -> _Session:
        self.log.opened += 1
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
        if self.log.fail_on and self.log.fail_on in sql:
            raise self.log.error
        if "tb_claim_tenant_deletion" in sql:
            return _Result(self.log.claim_rows)
        return _Result([], scalar=3)


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


class _Log:
    def __init__(self) -> None:
        self.statements: list[tuple[str, dict[str, Any], Any]] = []
        self.opened = 0
        self.committed = 0
        self.rolled_back = 0
        self.fail_on = ""
        self.error: Exception = RuntimeError("unset")
        self.claim_rows: list[dict[str, Any]] = []


def _store(log: _Log) -> SqlDeletionStore:
    return SqlDeletionStore(cast(async_sessionmaker[AsyncSession], lambda: _Session(log)))


def _db_error(sqlstate: str) -> DBAPIError:
    orig = type("PgError", (Exception,), {"sqlstate": sqlstate})("boom")
    return DBAPIError("stmt", {}, orig)


def _row(**overrides: Any) -> dict[str, Any]:  # noqa: ANN401
    rec = record(state="platform", attempts=2, fence=7)
    row: dict[str, Any] = {
        "id": rec.id,
        "tenant_id": rec.tenant_id,
        "host": rec.host,
        "installation_id": rec.installation_id,
        "delete_platform_resources": True,
        "state": "platform",
        "step_cursor": '{"after": 4}',
        "counts": {"a": 1},
        "skipped": "[]",
        "fence": 7,
        "attempts": 2,
    }
    row.update(overrides)
    return row


async def test_claim_maps_a_row_decoding_json_text_and_values() -> None:
    """jsonb arrives as text or decoded; both map to the record."""
    log = _Log()
    log.claim_rows = [_row()]
    rec = await _store(log).claim("h-1", 60)
    assert rec is not None
    assert (rec.state, rec.fence, rec.attempts, rec.delete_platform_resources) == (
        "platform",
        7,
        2,
        True,
    )
    assert rec.step_cursor == {"after": 4}
    assert rec.counts == {"a": 1}
    assert rec.skipped == []
    assert log.statements[0][1] == {"holder": "h-1", "ttl": 60}
    assert log.statements[0][2] is None  # host-scoped: no tenant context


async def test_claim_without_a_runnable_deletion_returns_none() -> None:
    """No row, no record."""
    assert await _store(_Log()).claim("h", 60) is None


async def test_renew_release_and_retry_send_the_lease_proof() -> None:
    """Every guarded call carries (id, holder, fence)."""
    log = _Log()
    store = _store(log)
    await store.renew(LEASE, 60)
    await store.release(LEASE)
    assert await store.retry(LEASE, "oops", 20) == 3
    sent = [(s[0], s[1]) for s in log.statements]
    assert "tb_renew_tenant_deletion" in sent[0][0]
    assert sent[0][1] == {"id": LEASE.id, "holder": "h-1", "fence": 7, "ttl": 60}
    assert "tb_release_tenant_deletion" in sent[1][0]
    assert "tb_retry_tenant_deletion" in sent[2][0]
    assert sent[2][1]["error"] == "oops"
    assert sent[2][1]["delay"] == 20
    assert log.committed == 3


async def test_advance_and_its_events_are_one_transaction_in_the_tenant_context() -> None:
    """ADR 0034 inv. 12: the transition and its audit insert commit together."""
    log = _Log()
    rec = record(state="platform")
    events = [audit.step_event(rec, "github", {"a": 1})]
    await _store(log).advance(
        LEASE, TENANT, "github", cursor=None, counts={"a": 1}, skipped=[], events=events
    )
    assert log.opened == 1
    assert log.committed == 1
    (advance, insert) = log.statements
    assert "tb_advance_tenant_deletion" in advance[0]
    assert json.loads(advance[1]["counts"]) == {"a": 1}
    assert advance[1]["cursor"] is None
    assert "audit_events" in insert[0]
    assert insert[1]["action"] == "tenant.deletion.step"
    assert advance[2] == TENANT == insert[2]


async def test_an_advance_runs_in_the_given_tenant_context_even_without_events() -> None:
    """The tenant is explicit (NG6h-4a-3), never read from `events[0]`."""
    log = _Log()
    await _store(log).advance(
        LEASE, TENANT, "platform", cursor={"n": 1}, counts={}, skipped=[], events=[]
    )
    assert log.statements[0][2] == TENANT
    assert json.loads(log.statements[0][1]["cursor"]) == {"n": 1}


async def test_a_failure_without_events_still_runs_in_the_tenant_context() -> None:
    """`fail` takes the tenant explicitly too."""
    log = _Log()
    await _store(log).fail(LEASE, TENANT, "bad", [])
    assert log.statements[0][2] == TENANT


async def test_finish_deletes_the_tenant_then_finishes_then_writes_completed() -> None:
    """The `completed` insert is in the same transaction as the tenant row's removal."""
    log = _Log()
    rec = record(state="database")
    await _store(log).finish(
        LEASE, rec, counts={"a": 1}, skipped=[], events=[audit.completed_event(rec, {"a": 1})]
    )
    order = [s[0] for s in log.statements]
    assert "tb_delete_tenant" in order[0]
    assert "tb_finish_tenant_deletion" in order[1]
    assert "audit_events" in order[2]
    assert log.committed == 1
    assert {s[2] for s in log.statements} == {TENANT}
    assert log.statements[0][1]["tenant_id"] == TENANT


async def test_fail_writes_the_reason_and_its_event() -> None:
    """One transaction for the terminal state and the `failed` event."""
    log = _Log()
    rec = record(state="github")
    await _store(log).fail(LEASE, TENANT, "bad", [audit.failed_event(rec, "bad")])
    assert "tb_fail_tenant_deletion" in log.statements[0][0]
    assert {s[2] for s in log.statements} == {TENANT}
    assert log.statements[0][1]["reason"] == "bad"
    assert "audit_events" in log.statements[1][0]
    assert log.committed == 1


@pytest.mark.parametrize("sqlstate", ["TB003", "TB004"])
async def test_a_lost_lease_and_a_terminal_record_both_mean_stop(sqlstate: str) -> None:
    """Both SQLSTATEs raise `LeaseLost`, and the transaction rolls back (no audit row)."""
    log = _Log()
    log.fail_on = "tb_advance_tenant_deletion"
    log.error = _db_error(sqlstate)
    rec = record(state="platform")
    with pytest.raises(LeaseLost):
        await _store(log).advance(
            LEASE,
            TENANT,
            "github",
            cursor=None,
            counts={},
            skipped=[],
            events=[audit.step_event(rec, "github", {})],
        )
    assert log.rolled_back == 1
    assert log.committed == 0
    assert not any("audit_events" in s[0] for s in log.statements)


async def test_any_other_database_error_propagates_as_is() -> None:
    """55000 (rows left for the tenant) is a transient error for the engine, not a stop."""
    log = _Log()
    log.fail_on = "tb_delete_tenant"
    log.error = _db_error("55000")
    with pytest.raises(DBAPIError):
        await _store(log).finish(LEASE, record(state="database"), counts={}, skipped=[], events=[])
