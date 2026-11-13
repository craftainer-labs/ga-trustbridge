"""The transaction time bound and its check against a real Postgres (ADR 0032, ADR 0034 B8).

Runs in CI's stack (integration). The `after_begin` hook applies the bound through
`SET LOCAL`, so a session opened from the app's own URL sees it; the real `TimeoutGate` reads the
effective values from the catalog. Tests that need PostgreSQL 17's `transaction_timeout` skip on an
older server, and the fallback test skips on a newer one.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import Settings
from trustbridge.tenancy import TimeoutGate, hook
from trustbridge.tenancy.hook import configure_transaction_timeout, install_tenant_hook

pytestmark = pytest.mark.integration


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker]:  # type: ignore[type-arg]
    """A session factory on the app's database; the hook's bound is restored afterwards."""
    s = Settings()
    url = URL.create(
        "postgresql+asyncpg",
        username=s.postgres_user,
        password=s.postgres_password,
        host=s.postgres_host,
        port=s.postgres_port,
        database=s.postgres_db,
    )
    engine: AsyncEngine = create_async_engine(url, poolclass=NullPool)
    saved = hook._timeout_ms
    install_tenant_hook()
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        hook._timeout_ms = saved
        await engine.dispose()


async def _version(factory: async_sessionmaker) -> int:  # type: ignore[type-arg]
    async with factory() as session:
        return int((await session.execute(text("SHOW server_version_num"))).scalar_one())


async def _require(factory: async_sessionmaker, *, at_least_17: bool) -> None:  # type: ignore[type-arg]
    if (await _version(factory) >= 170000) != at_least_17:
        pytest.skip("needs PostgreSQL 17 or later" if at_least_17 else "needs PostgreSQL before 17")


async def test_a_transaction_that_outlives_the_timeout_is_aborted(
    factory: async_sessionmaker,  # type: ignore[type-arg]
) -> None:
    """Two statements each under the bound that together exceed it: the transaction is aborted."""
    await _require(factory, at_least_17=True)
    configure_transaction_timeout(1)
    async with factory() as session:
        with pytest.raises(DBAPIError):
            async with session.begin():
                await session.execute(text("SELECT pg_sleep(0.6)"))
                await session.execute(text("SELECT pg_sleep(0.6)"))
                await session.execute(text("SELECT 1"))


async def test_a_transaction_within_the_timeout_commits_and_the_setting_does_not_leak(
    factory: async_sessionmaker,  # type: ignore[type-arg]
) -> None:
    """The bound is transaction-local: it is on inside the transaction and gone after it."""
    await _require(factory, at_least_17=True)
    configure_transaction_timeout(5)
    async with factory() as session, session.begin():
        inside = (await session.execute(text("SHOW transaction_timeout"))).scalar_one()
    assert inside == "5s"
    configure_transaction_timeout(None)
    async with factory() as session:
        after = (await session.execute(text("SHOW transaction_timeout"))).scalar_one()
    assert after in {"0", "0ms"}  # the server's own default, not the earlier SET LOCAL


async def test_before_17_the_fallback_aborts_a_statement_over_the_bound(
    factory: async_sessionmaker,  # type: ignore[type-arg]
) -> None:
    """PostgreSQL 16: `statement_timeout` ends a statement that runs past the bound."""
    await _require(factory, at_least_17=False)
    configure_transaction_timeout(0.5)
    async with factory() as session:
        with pytest.raises(DBAPIError):
            async with session.begin():
                await session.execute(text("SELECT pg_sleep(1.5)"))


def _gate(factory: async_sessionmaker, lag: float) -> TimeoutGate:  # type: ignore[type-arg]
    return TimeoutGate(factory, lag)


async def test_the_check_passes_with_a_bound_below_the_lag(
    factory: async_sessionmaker,  # type: ignore[type-arg]
) -> None:
    """With the hook's bound at 15 s and a 30 s lag the gate is ready (17+: no warning)."""
    configure_transaction_timeout(15)
    gate = _gate(factory, 30)
    assert await gate.refresh()
    assert gate.current
    assert gate.reason is None


async def test_the_check_fails_when_the_lag_is_not_above_the_bound(
    factory: async_sessionmaker,  # type: ignore[type-arg]
) -> None:
    """A lag at or below the bound is refused, naming the lag."""
    configure_transaction_timeout(15)
    gate = _gate(factory, 15)
    assert not await gate.refresh()
    assert gate.reason is not None
    assert "AUDIT_EXPORT_LAG_SECONDS" in gate.reason


async def test_the_check_fails_when_no_bound_is_applied(
    factory: async_sessionmaker,  # type: ignore[type-arg]
) -> None:
    """Without the hook's bound (and no server default) the transaction is unbounded: refused."""
    configure_transaction_timeout(None)
    async with factory() as session:
        names = ("transaction_timeout", "statement_timeout", "idle_in_transaction_session_timeout")
        values = [
            (await session.execute(text(f"SELECT current_setting('{n}', true)"))).scalar_one()
            for n in names
        ]
    if any(v not in (None, "0", "0ms") for v in values):
        pytest.skip("the server sets a default timeout of its own")
    gate = _gate(factory, 30)
    assert not await gate.refresh()
    assert gate.reason is not None


async def test_a_stricter_role_level_timeout_is_not_loosened(
    factory: async_sessionmaker,  # type: ignore[type-arg]
) -> None:
    """With `ALTER ROLE ... SET transaction_timeout = 2s` and a 15 s bound, 2 s stays in force."""
    await _require(factory, at_least_17=True)
    async with factory() as session:
        role = (await session.execute(text("SELECT current_user"))).scalar_one()
    admin = factory.kw["bind"]
    quoted = '"' + role.replace('"', '""') + '"'
    async with admin.connect() as conn:
        await conn.execute(text(f"ALTER ROLE {quoted} SET transaction_timeout = '2s'"))
        await conn.commit()
    try:
        configure_transaction_timeout(15)
        async with factory() as session, session.begin():
            inside = (await session.execute(text("SHOW transaction_timeout"))).scalar_one()
        assert inside == "2s"
        configure_transaction_timeout(1)  # a stricter configured bound still wins
        async with factory() as session, session.begin():
            inside = (await session.execute(text("SHOW transaction_timeout"))).scalar_one()
        assert inside == "1s"
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f"ALTER ROLE {quoted} RESET transaction_timeout"))
            await conn.commit()
