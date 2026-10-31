"""Out-of-request-path maintenance jobs -- currently just purging old archived rows.

Deliberately a flat module invoked externally (`python -m app.maintenance`), not
something the app schedules itself: this devcontainer-only stack has no
scheduler/worker service today (see ../../.devcontainer/compose.yml), and adding
one would be new shared infrastructure, not a request-path change. A real
deployment wires this into its own host/k8s CronJob on whatever schedule it
chooses -- see app.config.Settings.archive_purge_after_days, which defaults to
None (disabled) for exactly that reason.

`purge_archived` needs no per-resource wiring: it walks SQLAlchemy's own mapper
registry (`Base.registry.mappers`) for every model carrying `archived_at` (see
craftainer_crud.models.mixins.Archivable), the same `hasattr` detection
craftainer_crud.repositories.
sqlalchemy/craftainer_crud.repositories.memory already use, so a future Archivable model is
purged automatically the moment it's added -- no line here needs to change.

This does *not* introduce an operational dependency on an external scheduler:
`archive_purge_after_days` defaults to `None` (disabled), so an instance of this
template that never configures it, or never runs `python -m app.maintenance` at
all, behaves exactly as if purge didn't exist -- archived rows just accumulate
until something chooses to run it, on whatever cadence (or none) that
deployment picks. Revision history rows (see craftainer_crud.models.revision.Revision) are
left out of `purge_archived` for the same "not blocking the Hero demo" reason
its own storage growth is unbounded by this module -- a retention policy for
those, if a high-write-volume resource ever needs one, belongs here too, folded
in alongside archived-row purging rather than as a separate job.

This whole module is omitted from the coverage report (see
`[tool.coverage.run] omit` in `../../pyproject.toml`), for the same reason as
craftainer_crud.repositories.sqlalchemy's module docstring explains its own narrower
per-line pragmas: it's invoked externally (`python -m app.maintenance`), never
imported by the running app or by any `tests/e2e` journey against it, so
nothing here -- not even its own top-level imports -- ever executes in that
process. tests/integration/test_maintenance.py imports and calls
`purge_archived` directly instead, so its correctness is still verified by a
real test; omitting the file only means its line count no longer has to be
100%-covered by either `pytest` (tests/unit + tests/integration) or the
separate `pytest tests/e2e` run.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from craftainer_crud.models.base import Base
from craftainer_crud.models.revision import Revision  # noqa: F401 -- see above
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import session_factory
from app.models.hero import Hero  # noqa: F401 -- registers Hero on Base.registry.mappers

logger = logging.getLogger(__name__)


async def purge_archived(session: AsyncSession, *, older_than: datetime) -> int:
    """Hard-delete every archived row (across every Archivable model) past `older_than`.

    Iterates `Base.registry.mappers` rather than a hardcoded model list, so it
    needs no update when a future resource adopts craftainer_crud.models.mixins.Archivable.
    Returns the total number of rows deleted, across every model. Does not commit
    -- the caller (`_run` below, or a test's own fixture session) controls that.
    """
    total_deleted = 0
    for mapper in Base.registry.mappers:
        model = mapper.class_
        if not hasattr(model, "archived_at"):
            continue
        result = await session.execute(
            delete(model)
            .where(model.archived_at.is_not(None))
            .where(model.archived_at <= older_than)
        )
        # session.execute's static return type (Result[Any]) doesn't expose rowcount,
        # but a Core DELETE always executes as a CursorResult at runtime, which does.
        total_deleted += result.rowcount  # type: ignore[attr-defined]
    return total_deleted


async def _run() -> None:  # pragma: no cover -- thin CLI entrypoint, see __main__ below
    """Purge archived rows older than `settings.archive_purge_after_days`, then commit."""
    settings = get_settings()
    if settings.archive_purge_after_days is None:
        logger.warning("archive_purge_after_days is not set -- purge_archived was not run")
        return
    older_than = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        days=settings.archive_purge_after_days
    )
    async with session_factory() as session:
        deleted = await purge_archived(session, older_than=older_than)
        await session.commit()
    logger.info("purge_archived deleted %d row(s) archived before %s", deleted, older_than)


if __name__ == "__main__":  # pragma: no cover -- thin CLI entrypoint
    asyncio.run(_run())
