"""The runtime role safety check against a real Postgres (ADR 0031 invariant 16).

Runs in CI's stack (integration). A throwaway database holds a forced-RLS `audit_events` table
(the name the check treats as an RLS table); cluster roles with a unique suffix are created for
the test and dropped afterwards: one with `BYPASSRLS` and one plain login role. Each connects
through the real `RoleGate` and is judged by the real catalog query.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import Settings
from trustbridge.tenancy import RoleGate
from trustbridge.tenancy.role_check import RLS_TABLES

pytestmark = pytest.mark.integration

PASSWORD = "tb-role-check-test"  # noqa: S105 -- a throwaway role in a throwaway database


@dataclass(slots=True)
class Scratch:
    """A provisioned database, its roles and an admin engine."""

    name: str
    bypass: str
    normal: str
    admin: AsyncEngine
    server: AsyncEngine


def _url(user: str, password: str, database: str) -> URL:
    s = Settings()
    return URL.create(
        "postgresql+asyncpg",
        username=user,
        password=password,
        host=s.postgres_host,
        port=s.postgres_port,
        database=database,
    )


@pytest.fixture
async def scratch() -> AsyncIterator[Scratch]:
    """A scratch database with the seven unforced RLS tables and two login roles."""
    s = Settings()
    suffix = uuid4().hex[:10]
    name, bypass, normal = f"tb_role_{suffix}", f"tb_rb_{suffix}", f"tb_rn_{suffix}"
    server = create_async_engine(
        _url(s.postgres_user, s.postgres_password, s.postgres_db),
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    created: list[str] = []
    try:
        async with server.connect() as conn:
            await conn.execute(text(f"CREATE ROLE {bypass} LOGIN BYPASSRLS PASSWORD '{PASSWORD}'"))
            created.append(bypass)
            await conn.execute(text(f"CREATE ROLE {normal} LOGIN PASSWORD '{PASSWORD}'"))
            created.append(normal)
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    except DBAPIError:
        await _drop(server, name, created)
        await server.dispose()
        pytest.skip("the test database user cannot create roles and databases")
    admin = create_async_engine(
        _url(s.postgres_user, s.postgres_password, name), poolclass=NullPool
    )
    try:
        async with admin.begin() as conn:
            for table in RLS_TABLES:
                await conn.execute(text(f"CREATE TABLE {table} (tenant_id uuid)"))
                await conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        yield Scratch(name, bypass, normal, admin, server)
    finally:
        await admin.dispose()
        await _drop(server, name, created)
        await server.dispose()


async def _drop(server: AsyncEngine, name: str, roles: list[str]) -> None:
    async with server.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        for role in roles:
            await conn.execute(text(f"DROP ROLE IF EXISTS {role}"))


async def _gate(scratch: Scratch, role: str) -> tuple[RoleGate, AsyncEngine]:
    engine = create_async_engine(_url(role, PASSWORD, scratch.name), poolclass=NullPool)
    return RoleGate(async_sessionmaker(engine, expire_on_commit=False)), engine


async def _force(scratch: Scratch) -> None:
    async with scratch.admin.begin() as conn:
        await conn.execute(text("ALTER TABLE audit_events FORCE ROW LEVEL SECURITY"))


async def test_a_bypassrls_role_is_not_ready_once_a_table_is_forced(scratch: Scratch) -> None:
    """Connected as a BYPASSRLS role with `audit_events` forced: not ready, with the reason."""
    await _force(scratch)
    gate, engine = await _gate(scratch, scratch.bypass)
    try:
        assert not await gate.refresh()
        assert not gate.current
        assert gate.reason is not None
        assert scratch.bypass in gate.reason
        assert "BYPASSRLS" in gate.reason
    finally:
        await engine.dispose()


async def test_a_normal_role_is_ready_once_a_table_is_forced(scratch: Scratch) -> None:
    """Connected as a plain login role with `audit_events` forced: ready."""
    await _force(scratch)
    gate, engine = await _gate(scratch, scratch.normal)
    try:
        assert await gate.refresh()
        assert gate.current
        assert gate.reason is None
    finally:
        await engine.dispose()


async def test_the_check_is_skipped_while_no_table_is_forced(scratch: Scratch) -> None:
    """RLS enabled but not forced (release N): a BYPASSRLS role is accepted."""
    gate, engine = await _gate(scratch, scratch.bypass)
    try:
        assert await gate.refresh()
    finally:
        await engine.dispose()


async def test_a_superuser_is_not_ready_once_a_table_is_forced(scratch: Scratch) -> None:
    """The test database's own user, when it is a superuser, is refused like BYPASSRLS."""
    async with scratch.admin.connect() as conn:
        is_super = (
            await conn.execute(text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"))
        ).scalar_one()
    if not is_super:
        pytest.skip("the test database user is not a superuser")
    await _force(scratch)
    gate = RoleGate(async_sessionmaker(scratch.admin, expire_on_commit=False))
    assert not await gate.refresh()
    assert gate.reason is not None
    assert "a superuser" in gate.reason


async def test_a_missing_table_is_not_ready_even_for_a_normal_role(scratch: Scratch) -> None:
    """A table the check expects but cannot find fails closed and is named."""
    async with scratch.admin.begin() as conn:
        await conn.execute(text("DROP TABLE scope_leases"))
    gate, engine = await _gate(scratch, scratch.normal)
    try:
        assert not await gate.refresh()
        assert gate.reason is not None
        assert "scope_leases" in gate.reason
    finally:
        await engine.dispose()


async def test_tables_in_another_schema_are_not_found(scratch: Scratch) -> None:
    """Tables outside `APP_SCHEMA` (even if on the search_path) are not seen: not ready."""
    async with scratch.admin.begin() as conn:
        await conn.execute(text("CREATE SCHEMA other"))
        await conn.execute(text("ALTER TABLE audit_events SET SCHEMA other"))
        await conn.execute(text("ALTER TABLE other.audit_events FORCE ROW LEVEL SECURITY"))
        await conn.execute(text(f"ALTER ROLE {scratch.bypass} SET search_path = other, public"))
    gate, engine = await _gate(scratch, scratch.bypass)
    try:
        assert not await gate.refresh()
        assert gate.reason is not None
        assert "audit_events" in gate.reason
    finally:
        await engine.dispose()


async def test_a_forced_partitioned_table_arms_the_check(scratch: Scratch) -> None:
    """A partitioned table (relkind 'p') counts: a BYPASSRLS role is refused."""
    async with scratch.admin.begin() as conn:
        await conn.execute(text("DROP TABLE audit_events"))
        await conn.execute(
            text("CREATE TABLE audit_events (tenant_id uuid) PARTITION BY LIST (tenant_id)")
        )
        await conn.execute(text("ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY"))
        await conn.execute(text("ALTER TABLE audit_events FORCE ROW LEVEL SECURITY"))
    gate, engine = await _gate(scratch, scratch.bypass)
    try:
        assert not await gate.refresh()
        assert gate.reason is not None
        assert "BYPASSRLS" in gate.reason
    finally:
        await engine.dispose()
