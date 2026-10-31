"""Integration test: build_engine's `local` pool mode against the real Postgres service."""

from sqlalchemy import text
from sqlalchemy.pool import AsyncAdaptedQueuePool

from app.config import Settings
from app.db import build_engine


async def test_local_pool_reuses_one_connection_within_one_event_loop() -> None:
    """Sequential checkouts under `local` mode get the same backend; the pool holds it idle."""
    engine = build_engine(Settings(db_pool_mode="local", db_pool_size=2, db_max_overflow=0))
    try:
        assert isinstance(engine.pool, AsyncAdaptedQueuePool)
        pids = []
        for _ in range(3):
            async with engine.connect() as conn:
                pids.append((await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one())
        assert len(set(pids)) == 1
        assert engine.pool.checkedin() == 1
    finally:
        await engine.dispose()


async def test_app_engine_reports_tls_state_matching_postgres_sslmode() -> None:
    """The app's own engine connects over TLS exactly when POSTGRES_SSLMODE asks for it.

    Smoke stack check (POSTGRES_SSLMODE=require): the single row must be `True`.
    In the devcontainer (sslmode unset) the server may not offer TLS, so this only
    asserts the wiring when an encrypting mode is configured.
    """
    settings = Settings()
    engine = build_engine(settings)
    try:
        async with engine.connect() as conn:
            ssl_used = (
                await conn.execute(text("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()"))
            ).scalar_one()
    finally:
        await engine.dispose()
    if settings.postgres_sslmode in ("require", "verify-ca", "verify-full"):
        assert ssl_used is True
