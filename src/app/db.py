"""The app's async SQLAlchemy engine and session factory, built from `Settings`."""

from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool

from app.config import Settings, get_settings


def build_engine(settings: Settings) -> AsyncEngine:
    """Build the async engine for `settings.db_pool_mode`, SSL mode and timeouts.

    - `null` (default): NullPool. An asyncpg connection is tied to the event loop it
      was opened on, and pooling would hand one from a closed-out loop to a new one --
      fine for the app's own single long-lived event loop, but tests/integration
      mixes pytest-asyncio's loop with TestClient's own background-thread loop, which
      pooling can't survive ("cannot perform operation: another operation is in
      progress"). A fresh connection per checkout sidesteps that everywhere, so this
      stays the default and the test mode, at the cost of connection reuse.
    - `local`: an in-process AsyncAdaptedQueuePool (pre-pinged), for an app that talks
      straight to Postgres in a single long-lived event loop. Pool size, overflow and
      recycle are passed only when set; otherwise SQLAlchemy's defaults apply.
    - `external`: NullPool behind a transaction-mode pooler such as PgBouncer, with
      unique prepared-statement names, because asyncpg's default numbered names
      collide across clients sharing a server connection.

    `postgres_sslmode` goes to asyncpg's `ssl` (which accepts the libpq mode strings),
    `db_connect_timeout` to `timeout` and `db_command_timeout` to `command_timeout`,
    each only when set.
    """
    connect_args: dict[str, Any] = {}
    if settings.postgres_sslmode is not None:
        connect_args["ssl"] = settings.postgres_sslmode
    if settings.db_connect_timeout is not None:
        connect_args["timeout"] = settings.db_connect_timeout
    if settings.db_command_timeout is not None:
        connect_args["command_timeout"] = settings.db_command_timeout

    kwargs: dict[str, Any] = {}
    if settings.db_pool_mode == "local":
        kwargs["poolclass"] = AsyncAdaptedQueuePool
        kwargs["pool_pre_ping"] = True
        if settings.db_pool_size is not None:
            kwargs["pool_size"] = settings.db_pool_size
        if settings.db_max_overflow is not None:
            kwargs["max_overflow"] = settings.db_max_overflow
        if settings.db_pool_recycle is not None:
            kwargs["pool_recycle"] = settings.db_pool_recycle
    else:
        kwargs["poolclass"] = NullPool
        # `# pragma: no cover`: tests/e2e covers null (bare leg) and local (dev and mock
        # legs); this pooler mode is unit-tested in tests/unit/test_db.py only.
        if settings.db_pool_mode == "external":  # pragma: no cover
            connect_args["prepared_statement_name_func"] = lambda: f"__asyncpg_{uuid4()}__"

    if connect_args:
        kwargs["connect_args"] = connect_args
    return create_async_engine(settings.database_url, **kwargs)


engine: AsyncEngine = build_engine(get_settings())
session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False
)
