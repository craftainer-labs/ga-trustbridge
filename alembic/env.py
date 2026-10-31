"""Alembic environment: autogenerate against craftainer_crud.models.base metadata.

Migrates over asyncpg.
"""

import asyncio
import logging
from logging.config import fileConfig

from craftainer_crud.models import (
    revision,  # noqa: F401 -- import registers Revision on Base.metadata
)
from craftainer_crud.models.base import Base
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from alembic import context
from app.config import get_settings
from app.models import hero  # noqa: F401 -- import registers Hero on Base.metadata

config = context.config

# Configure alembic's own logging only when running standalone (`alembic upgrade` from
# the CLI), where the root logger has no handlers yet. Invoked from the app (lifespan
# migrations) the root logger already carries the JSON/OTLP handlers from
# `configure_logging()`, and `fileConfig` would replace them with alembic.ini's stderr
# handler. `disable_existing_loggers=False` keeps already-created app.* loggers enabled.
if config.config_file_name is not None and not logging.getLogger().handlers:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit migration SQL against a URL only, without a live database connection."""
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    """Configure the migration context against a live connection and run migrations."""
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    """Connect over asyncpg and run migrations through the sync-style API via run_sync."""
    connectable: AsyncEngine = create_async_engine(get_settings().database_url)

    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations against a live database, bridging into the async engine."""
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
