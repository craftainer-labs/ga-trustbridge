"""`SqlRecordPurger` over a scripted session: bounds, batching, the budget and the metric (the
Postgres half is in `tests/trustbridge/tenancy/test_deletion_purge_integration.py`)."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace, TracebackType
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.deletion import metrics
from trustbridge.deletion.ports import PurgeResult
from trustbridge.deletion.purge import SqlRecordPurger

NOW = datetime(2026, 6, 1, tzinfo=UTC)


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

    async def execute(self, statement: object, params: dict[str, Any]) -> Any:  # noqa: ANN401
        self.log.calls.append(params)
        purged, unprotected = self.log.script.pop(0)
        return SimpleNamespace(one=lambda: SimpleNamespace(purged=purged, unprotected=unprotected))


class _Tx:
    def __init__(self, log: _Log) -> None:
        self.log = log

    async def __aenter__(self) -> None:
        self.log.transactions += 1

    async def __aexit__(
        self, kind: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        return None


class _Log:
    def __init__(self, *script: tuple[int, int]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.transactions = 0

    def factory(self) -> async_sessionmaker[AsyncSession]:
        return cast("async_sessionmaker[AsyncSession]", lambda: _Session(self))


def _purger(log: _Log, **over: Any) -> SqlRecordPurger:  # noqa: ANN401
    args: dict[str, Any] = {
        "retention_days": 400,
        "min_days": 90,
        "batch_size": 5,
        "max_batches": 3,
        "clock": lambda: NOW,
    }
    return SqlRecordPurger(log.factory(), **{**args, **over})


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"retention_days": -1}, "must not be negative"),
        ({"retention_days": 30}, "at least min_days"),
        ({"min_days": 0, "retention_days": 0}, "min_days must be at least 1"),
        ({"batch_size": 0}, "batch_size"),
        ({"batch_size": 10001}, "batch_size"),
        ({"max_batches": 0}, "max_batches"),
    ],
)
def test_the_bounds_fail_closed_before_any_transaction(over: dict[str, Any], message: str) -> None:
    """A negative or too-short retention, or a bad batch bound, is refused at construction."""
    log = _Log()
    with pytest.raises(ValueError, match=message):
        _purger(log, **over)
    assert log.transactions == 0


async def test_retention_zero_never_touches_the_database() -> None:
    """The default keeps every record for ever."""
    log = _Log()
    assert await _purger(log, retention_days=0).purge() == PurgeResult()
    assert log.transactions == 0


async def test_it_runs_batches_until_a_short_one_and_reports_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One transaction per batch, the clock sets the cut-off, the metric and `on_batch` fire."""
    added: list[int] = []
    monkeypatch.setattr(metrics.records_purged, "add", added.append)
    log = _Log((5, 0), (5, 0), (2, 1))
    seen: list[int] = []
    result = await _purger(log).purge(seen.append)
    assert result == PurgeResult(12, 1)
    assert seen == [5, 5, 2]
    assert added == [5, 5, 2]
    assert log.transactions == 3
    assert log.calls[0] == {
        "before": NOW - timedelta(days=400),
        "min_days": 90,
        "batch": 5,
    }


async def test_it_stops_at_the_batch_count_and_skips_the_metric_for_an_empty_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At most `max_batches` run per call; a batch that purged nothing adds no metric."""
    added: list[int] = []
    monkeypatch.setattr(metrics.records_purged, "add", added.append)
    log = _Log((5, 0), (5, 0), (5, 0), (5, 0))
    assert (await _purger(log).purge()).purged == 15
    assert log.transactions == 3
    empty = _Log((0, 2))
    assert await _purger(empty).purge() == PurgeResult(0, 2)
    assert added == [5, 5, 5]


async def test_it_stops_starting_batches_once_the_budget_has_elapsed() -> None:
    """The lease budget: no new batch after `budget_seconds`; the next call continues."""
    ticks = iter([0.0, 1.0, 11.0])
    clock: Callable[[], float] = lambda: next(ticks)  # noqa: E731
    log = _Log((5, 0), (5, 0))
    result = await _purger(log, budget_seconds=10.0, monotonic=clock).purge()
    assert result.purged == 5
    assert log.transactions == 1
