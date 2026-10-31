"""Unit test: alembic/env.py leaves the app's root logging alone, and logs when standalone."""

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from craftainer_observability.logs import _JSONFormatter, configure_logging

from alembic import command

_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def _restore_logging() -> Iterator[None]:
    """Save/restore the root logger's handlers and level -- shared process-wide state."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers, root.level = handlers, level


def _offline_upgrade() -> None:
    """Run alembic/env.py in offline mode (renders SQL, needs no database)."""
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "alembic"))
    command.upgrade(config, "head", sql=True)


def test_env_keeps_configured_root_handlers(_restore_logging: None) -> None:
    """With a configured root logger (the app), running env.py leaves its handlers alone."""
    root = logging.getLogger()
    root.handlers.clear()
    configure_logging()
    before = list(root.handlers)
    _offline_upgrade()
    assert root.handlers == before
    assert any(isinstance(h.formatter, _JSONFormatter) for h in root.handlers)


def test_env_configures_logging_when_standalone(_restore_logging: None) -> None:
    """With no root handlers (the alembic CLI), env.py applies alembic.ini's logging."""
    root = logging.getLogger()
    root.handlers.clear()
    _offline_upgrade()
    assert root.handlers


def test_configure_logging_routes_uvicorn_loggers_to_root(_restore_logging: None) -> None:
    """uvicorn's own plain-text handlers are dropped so its records reach the JSON handler."""
    names = ("uvicorn", "uvicorn.error", "uvicorn.access")
    loggers = [logging.getLogger(n) for n in names]
    saved = [(lg, list(lg.handlers), lg.propagate) for lg in loggers]
    try:
        for lg in loggers:
            lg.addHandler(logging.StreamHandler())
            lg.propagate = False
        configure_logging()
        for lg in loggers:
            assert not lg.handlers
            assert lg.propagate
    finally:
        for lg, handlers, propagate in saved:
            lg.handlers = handlers
            lg.propagate = propagate


def test_configure_logging_keeps_disabled_uvicorn_access_log_silent(_restore_logging: None) -> None:
    """uvicorn's --no-access-log state (no handlers, propagate=False) is not re-enabled."""
    access = logging.getLogger("uvicorn.access")
    saved = (list(access.handlers), access.propagate)
    try:
        access.handlers = []
        access.propagate = False
        configure_logging()
        assert not access.handlers
        assert not access.propagate
    finally:
        access.handlers, access.propagate = saved
