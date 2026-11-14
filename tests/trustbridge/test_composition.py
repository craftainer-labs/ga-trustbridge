"""The deletion composition root (NG6h-7a)."""

import asyncio
import logging
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.trustbridge.fakes import FakeHostChecker, FakeRedis
from tests.trustbridge.helpers import as_redis, make_settings
from trustbridge import composition
from trustbridge.deletion.engine import DeletionEngine
from trustbridge.deletion.ports import DeletionWiring
from trustbridge.deletion.purge import SqlRecordPurger
from trustbridge.worker import runtime
from trustbridge.worker.runtime import ProcessOverrides, build_core


def _sessions() -> async_sessionmaker[AsyncSession]:
    """A session factory that is never opened: composing touches no database."""
    return async_sessionmaker()


def test_mock_mode_composes_nothing_and_logs_one_line(caplog: pytest.LogCaptureFixture) -> None:
    """Without a database no engine and no purger exist, and one line says so."""
    core = build_core(make_settings(), redis=as_redis(FakeRedis()), session_factory=None)
    with caplog.at_level(logging.INFO, logger=composition.logger.name):
        wiring = composition.compose_deletion(core, None)
    assert wiring == DeletionWiring()
    assert [r.getMessage() for r in caplog.records] == [
        "tenant deletion not composed: mock mode has no database"
    ]


def test_the_engine_and_the_purger_are_composed_from_the_settings(
    key_signers: None,
) -> None:
    """Every step slot is filled and the settings reach the engine, the step and the purger."""
    settings = make_settings(
        deletion_step_batch_size=7,
        deletion_lease_ttl_seconds=90,
        deletion_backoff_base_seconds=5,
        deletion_backoff_cap_seconds=50,
        deletion_budget_fraction=0.4,
        deletion_retention_days=100,
        deletion_purge_batch_size=11,
        deletion_purge_max_batches=3,
        lease_ttl_seconds=60,
    )
    sessions = _sessions()
    core = build_core(settings, redis=as_redis(FakeRedis()), session_factory=sessions)
    wiring = composition.compose_deletion(core, sessions, holder=lambda: "holder-1")
    engine = cast("Any", wiring.driver)
    assert isinstance(engine, DeletionEngine)
    config = engine._config
    assert (config.holder, config.lease_ttl_seconds, config.budget_fraction) == (
        "holder-1",
        90,
        0.4,
    )
    assert (config.backoff_base_seconds, config.backoff_cap_seconds) == (5, 50)
    assert cast("Any", engine._steps.database)._batch_size == 7
    assert cast("Any", engine._steps.drain)._max_passes == settings.deletion_drain_max_passes
    purger = cast("Any", wiring.record_purger)
    assert isinstance(purger, SqlRecordPurger)
    assert purger._budget_seconds == 30  # half LEASE_TTL_SECONDS, as for the audit purge
    assert (purger._retention_days, purger._batch_size, purger._max_batches) == (100, 11, 3)


async def test_the_reconciler_process_runs_with_the_composer_result(
    key_signers: None, caplog: pytest.LogCaptureFixture
) -> None:
    """`run_process` hands the composed driver and purger to the reconciler's duties."""
    seen: list[Any] = []

    class _Driver:
        async def run_once(self) -> bool:
            return False

    driver = _Driver()

    def composer(core: Any, sessions: Any) -> DeletionWiring:  # noqa: ANN401 -- a probe
        seen.append((core, sessions))
        return DeletionWiring(driver=driver)

    stop = asyncio.Event()
    stop.set()
    overrides = ProcessOverrides(
        redis=as_redis(FakeRedis()), checker=FakeHostChecker(), stop=stop, composer=composer
    )
    with caplog.at_level(logging.INFO):
        assert await runtime.run_process("reconciler", make_settings(), overrides) == 0
    assert len(seen) == 1
    assert "tenant deletion disabled: no deletion engine" not in caplog.text
    assert "deletion record purge disabled: no record purger" in caplog.text


def test_main_passes_the_composer(monkeypatch: pytest.MonkeyPatch) -> None:
    """`composition.main` runs the process kind with `compose_deletion`."""
    seen: list[tuple[str, object]] = []

    def fake(kind: str, runner: object = None, composer: object = None) -> int:
        seen.append((kind, composer))
        return 3

    monkeypatch.setattr(runtime, "main", fake)
    assert composition.main("reconciler") == 3
    assert seen == [("reconciler", composition.compose_deletion)]
