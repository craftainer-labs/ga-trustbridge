"""The composition root of the reconciler's tenant deletion (ADR 0034, NG6h-7a).

The deletion engine (`trustbridge.deletion`) must not import the worker, the adapters or the
GitHub core, and the worker reaches the engine only through the `DeletionDriver` Protocol
(import-linter). This module sits beside both and is the one place that imports the concrete
classes of each side and joins them: `python -m trustbridge.reconciler` runs the reconciler
process with `compose_deletion` as its composer.

It composes, from the process's `Core` and session factory:

- the engine (`DeletionEngine`) over the Postgres record store, with the four steps: the drain
  (`SqlWritersPort`), the platform step (`build_platform_port`), the GitHub step
  (`build_github_port`) and the database step (`DELETION_STEP_BATCH_SIZE` rows per pass);
- the retention purge of finished records (`SqlRecordPurger`), whose run budget is half
  `LEASE_TTL_SECONDS` like the audit purge, so it ends before the duty's lease could lapse.

Mock mode (no database) composes nothing and logs one line; the reconciler then logs that the
deletion duties are disabled. Every other failure to compose raises at startup.
"""

import logging
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.adapters.loader import load_adapter_classes
from trustbridge.deletion.database import SqlDatabaseStep
from trustbridge.deletion.drain import DrainStep
from trustbridge.deletion.engine import DeletionEngine, EngineConfig, EngineSteps
from trustbridge.deletion.github import GithubDeletionStep
from trustbridge.deletion.platform import AdapterPlatformStep
from trustbridge.deletion.ports import DeletionWiring
from trustbridge.deletion.purge import SqlRecordPurger
from trustbridge.deletion.sql import SqlDeletionStore
from trustbridge.worker import runtime
from trustbridge.worker.platform_deletion import ConnectionNames
from trustbridge.worker.runtime import Core, consumer_name
from trustbridge.worker.tenant_reconcile import (
    build_github_port,
    build_platform_port,
    build_writers_port,
)

logger = logging.getLogger(__name__)


def compose_deletion(
    core: Core,
    sessions: async_sessionmaker[AsyncSession] | None,
    *,
    holder: Callable[[], str] = consumer_name,
) -> DeletionWiring:
    """The deletion engine and the record purger for `core`, or neither in mock mode."""
    if sessions is None:
        logger.info("tenant deletion not composed: mock mode has no database")
        return DeletionWiring()
    s = core.settings
    classes = load_adapter_classes(s.adapters_enabled)
    writers = build_writers_port(core, sessions, classes)
    platform = build_platform_port(core, sessions, classes)
    github = build_github_port(core, sessions, classes)
    if writers is None or platform is None or github is None:  # pragma: no cover -- sessions given
        raise RuntimeError("the deletion ports need a database")
    steps = EngineSteps(
        drain=DrainStep(
            writers,
            poll_seconds=s.deletion_drain_poll_seconds,
            max_passes=s.deletion_drain_max_passes,
        ),
        platform=AdapterPlatformStep(platform, ConnectionNames(core.secrets)),
        github=GithubDeletionStep(github),
        database=SqlDatabaseStep(sessions, batch_size=s.deletion_step_batch_size),
    )
    config = EngineConfig(
        holder=holder(),
        lease_ttl_seconds=s.deletion_lease_ttl_seconds,
        backoff_base_seconds=s.deletion_backoff_base_seconds,
        backoff_cap_seconds=s.deletion_backoff_cap_seconds,
        budget_fraction=s.deletion_budget_fraction,
    )
    purger = SqlRecordPurger(
        sessions,
        retention_days=s.deletion_retention_days,
        min_days=s.deletion_retention_min_days,
        batch_size=s.deletion_purge_batch_size,
        max_batches=s.deletion_purge_max_batches,
        budget_seconds=s.lease_ttl_seconds / 2,
    )
    return DeletionWiring(DeletionEngine(SqlDeletionStore(sessions), steps, config), purger)


def main(kind: runtime.ProcessKind) -> int:
    """Run `kind` with the deletion composition root (`python -m trustbridge.reconciler`)."""
    return runtime.main(kind, composer=compose_deletion)
