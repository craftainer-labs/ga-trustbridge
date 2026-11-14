"""Unit test: alembic/env.py connects with the app's own engine (TLS mode and timeouts)."""

from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy.ext import asyncio as sqlalchemy_asyncio
from sqlalchemy.pool import NullPool

from alembic import command
from app import db
from app.config import Settings, get_settings

_ROOT = Path(__file__).resolve().parents[2]


class _EngineBuilt(Exception):  # noqa: N818 -- a test sentinel, not an error condition
    """Raised by the stand-in `build_engine` so no database is needed."""


def test_online_migrations_connect_with_the_apps_ssl_mode_and_connect_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The migration session gets `build_connect_args(...)`: same ssl and connect timeout."""
    settings = get_settings()
    monkeypatch.setattr(settings, "postgres_sslmode", "verify-full")
    monkeypatch.setattr(settings, "db_connect_timeout", 3.0)
    monkeypatch.setattr(settings, "db_command_timeout", 7.0)
    seen: dict[str, Any] = {}

    def _create_async_engine(url: str, **kwargs: Any) -> None:  # noqa: ANN401 -- engine kwargs
        seen.update(kwargs, url=url)
        raise _EngineBuilt

    monkeypatch.setattr(sqlalchemy_asyncio, "create_async_engine", _create_async_engine)
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "alembic"))
    with pytest.raises(_EngineBuilt):
        command.upgrade(config, "head")
    assert seen["url"] == settings.database_url
    # The request-path command timeout is not applied to DDL.
    assert seen["connect_args"] == {"ssl": "verify-full", "timeout": 3.0}
    assert seen["poolclass"] is NullPool


def test_connect_args_carry_the_ssl_mode_and_timeouts() -> None:
    """`build_connect_args` is the one place TLS mode and timeouts are mapped for asyncpg."""
    settings = Settings(
        mode="dev", postgres_sslmode="verify-full", db_connect_timeout=3, db_command_timeout=7
    )
    assert db.build_connect_args(settings) == {
        "ssl": "verify-full",
        "timeout": 3,
        "command_timeout": 7,
    }
    assert db.build_connect_args(settings, command_timeout=False) == {
        "ssl": "verify-full",
        "timeout": 3,
    }
    assert db.build_connect_args(Settings(mode="dev")) == {}
