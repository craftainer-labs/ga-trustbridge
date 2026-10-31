"""Unit tests for app.db.build_engine: pool class, connect_args and pool sizing per setting."""

from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool

from app import db
from app.config import Settings


def _captured_kwargs(**overrides: object) -> dict[str, Any]:
    """Build an engine from Settings(**overrides) and return the kwargs it was created with."""
    with patch.object(db, "create_async_engine") as create:
        db.build_engine(Settings.model_validate(overrides))
    return dict(create.call_args.kwargs)


def test_null_mode_is_default_nullpool_without_connect_args() -> None:
    """Null mode is default nullpool without connect args."""
    engine = db.build_engine(Settings())
    assert isinstance(engine.pool, NullPool)
    kwargs = _captured_kwargs()
    assert kwargs["poolclass"] is NullPool
    assert "connect_args" not in kwargs
    assert "pool_pre_ping" not in kwargs


def test_local_mode_uses_queue_pool_with_defaults_when_unset() -> None:
    """Local mode uses queue pool with defaults when unset."""
    engine = db.build_engine(Settings(db_pool_mode="local"))
    assert isinstance(engine.pool, AsyncAdaptedQueuePool)
    kwargs = _captured_kwargs(db_pool_mode="local")
    assert kwargs["pool_pre_ping"] is True
    for unset in ("pool_size", "max_overflow", "pool_recycle", "connect_args"):
        assert unset not in kwargs


def test_local_mode_passes_pool_sizing_when_set() -> None:
    """Local mode passes pool sizing when set."""
    kwargs = _captured_kwargs(
        db_pool_mode="local", db_pool_size=7, db_max_overflow=3, db_pool_recycle=600
    )
    assert kwargs["pool_size"] == 7
    assert kwargs["max_overflow"] == 3
    assert kwargs["pool_recycle"] == 600


def test_pool_sizing_is_ignored_outside_local_mode() -> None:
    """Pool sizing is ignored outside local mode."""
    kwargs = _captured_kwargs(db_pool_mode="null", db_pool_size=7)
    assert "pool_size" not in kwargs


def test_external_mode_uses_nullpool_with_unique_statement_names() -> None:
    """External mode uses nullpool with unique statement names."""
    engine = db.build_engine(Settings(db_pool_mode="external"))
    assert isinstance(engine.pool, NullPool)
    kwargs = _captured_kwargs(db_pool_mode="external")
    assert kwargs["poolclass"] is NullPool
    name_func = kwargs["connect_args"]["prepared_statement_name_func"]
    first, second = name_func(), name_func()
    assert first != second
    assert first.startswith("__asyncpg_")
    assert first.endswith("__")


@pytest.mark.parametrize("mode", ["null", "local"])
def test_prepared_statement_name_func_only_in_external_mode(mode: str) -> None:
    """Prepared statement name func only in external mode."""
    kwargs = _captured_kwargs(db_pool_mode=mode, postgres_sslmode="require")
    assert "prepared_statement_name_func" not in kwargs["connect_args"]


def test_ssl_and_timeouts_passed_to_asyncpg_only_when_set() -> None:
    """Ssl and timeouts passed to asyncpg only when set."""
    kwargs = _captured_kwargs(
        postgres_sslmode="require", db_connect_timeout=2.5, db_command_timeout=10
    )
    assert kwargs["connect_args"] == {"ssl": "require", "timeout": 2.5, "command_timeout": 10}


def test_each_connect_arg_is_independent() -> None:
    """Each connect arg is independent."""
    assert _captured_kwargs(db_command_timeout=4)["connect_args"] == {"command_timeout": 4}
    assert _captured_kwargs(db_connect_timeout=4)["connect_args"] == {"timeout": 4}
    assert _captured_kwargs(postgres_sslmode="verify-full")["connect_args"] == {
        "ssl": "verify-full"
    }


def test_module_level_engine_and_session_factory_exist() -> None:
    """Module level engine and session factory exist."""
    assert db.engine.pool.__class__ is NullPool
    assert db.session_factory.kw["bind"] is db.engine
