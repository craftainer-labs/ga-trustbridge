"""The migrate Job (ADR 0028 "Processes and database roles"; invariants 17-18).

`python -m trustbridge.worker migrate upgrade` runs Alembic's `upgrade head` as the role the
Job's `POSTGRES_*` variables name (the owner role), straight against the primary (the
advisory lock below is session-level and does not survive PgBouncer transaction mode, so this
is the one place an advisory lock is acceptable). Two concurrent runs serialise on the lock,
so their DDL never interleaves; a failed migration returns exit code 1 so the rollout does not
start. `MIGRATE_EXPECTED_ROLE`, when set, makes the Job refuse to run as any other role.

`revision` wraps Alembic's CLI after importing the trustbridge models, so autogenerate sees
their tables on the shared `Base` (ADR 0028 "Migrations").
"""

import asyncio
import logging
from collections.abc import Callable

from alembic.config import Config
from alembic.config import main as alembic_main
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from trustbridge.settings import TrustbridgeSettings, load_trustbridge_settings

logger = logging.getLogger(__name__)
MIGRATE_LOCK_KEY = 7_452_731_000_001


def _alembic_upgrade() -> None:
    """Blocking Alembic upgrade (its async env runs its own loop, so call it in a thread)."""
    command.upgrade(Config("alembic.ini"), "head")


async def run_upgrade(
    settings: TrustbridgeSettings,
    *,
    upgrade: Callable[[], None] = _alembic_upgrade,
) -> int:
    """Take the migration lock, check the role, upgrade; returns the process exit code."""
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.connect() as lock_conn:
            role = (await lock_conn.execute(text("SELECT current_user"))).scalar_one()
            expected = settings.migrate_expected_role
            if expected is not None and role != expected:
                logger.error("migrate must run as %s, not %s", expected, role)
                return 1
            await lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": MIGRATE_LOCK_KEY})
            logger.info("migrating", extra={"role": role})
            try:
                await asyncio.to_thread(upgrade)
            except Exception:
                logger.exception("migration failed")
                return 1
            finally:
                await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATE_LOCK_KEY})
    finally:
        await engine.dispose()
    return 0


def main(argv: list[str]) -> int:
    """`upgrade` (the Job) or `revision ...` (autogenerate wrapper); returns the exit code."""
    if not argv or argv[0] not in {"upgrade", "revision"}:
        logger.error("usage: migrate upgrade | migrate revision [alembic revision args]")
        return 2
    if argv[0] == "revision":
        from trustbridge.adapters import models as adapter_models  # noqa: PLC0415
        from trustbridge.github import models as github_models  # noqa: PLC0415
        from trustbridge.worker import models  # noqa: PLC0415

        # Importing registers every trustbridge table on the shared `Base` for autogenerate.
        del adapter_models, github_models, models

        alembic_main(argv=argv)
        return 0
    return asyncio.run(run_upgrade(load_trustbridge_settings()))
