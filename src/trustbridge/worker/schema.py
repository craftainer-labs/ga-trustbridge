"""The schema gate (ADR 0028 "Processes and database roles"; invariants 12-15).

Every migration writes the schema-compatibility integer (`schema_compat.version`, one row,
`GREATEST` so it never goes down); code declares the integer it requires
(`REQUIRED_SCHEMA_VERSION`). A process accepts a database whose integer is equal or greater
(rolled-back code against a newer schema) and waits for a lower one, without knowing the
database's Alembic revision id. While waiting, the api and the long-running processes stay
alive (liveness passes) and report not ready; they exit non-zero only after
`SCHEMA_WAIT_SECONDS`. With `MODE=mock` nothing waits and no database is touched.

A migration bumps the integer with `bump_schema_compat_sql(n)`'s statement (copied inline in
the revision; migrations never import application code).
"""

import asyncio
import logging
import os
import time
from collections.abc import Callable

from craftainer_observability.health.base import UNHEALTHY_DETAIL, HealthCheckResult
from craftainer_observability.health.registry import HealthRegistry
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.worker.models import SchemaCompat

logger = logging.getLogger(__name__)

REQUIRED_SCHEMA_VERSION = 1
SCHEMA_CHECK_NAME = "schema"


def bump_schema_compat_sql(version: int) -> str:
    """The statement every migration runs to record its schema-compatibility integer."""
    return (
        "INSERT INTO schema_compat (id, version) VALUES (1, "  # noqa: S608 -- constants and ints only
        f"{int(version)}) ON CONFLICT (id) DO UPDATE SET version = "
        f"GREATEST(schema_compat.version, {int(version)})"
    )


class SchemaWaitTimeout(Exception):  # noqa: N818 -- a terminal condition, named for what it says
    """The schema did not become current within `SCHEMA_WAIT_SECONDS`."""


class SchemaGate:
    """Reads the database's integer and compares it with the code's requirement."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None,
        *,
        required: int = REQUIRED_SCHEMA_VERSION,
        mock: bool = False,
    ) -> None:
        """`mock` (MODE=mock) makes the gate always current and never opens a session."""
        self._sf = session_factory
        self.required = required
        self._mock = mock
        self.current: bool = mock
        self.version: int | None = None

    async def read_version(self) -> int | None:
        """The database's integer; None if the table is missing or empty (not migrated)."""
        if self._sf is None:  # pragma: no cover -- only mock mode passes no factory
            return None
        try:
            async with self._sf() as session:
                return (await session.execute(select(SchemaCompat.version))).scalar_one_or_none()
        except Exception:
            logger.warning("schema compatibility table unreadable", exc_info=True)
            return None

    async def refresh(self) -> bool:
        """Re-read the integer and update `current`; returns it."""
        if self._mock:
            return True
        self.version = await self.read_version()
        self.current = self.version is not None and self.version >= self.required
        return self.current

    async def wait(
        self,
        *,
        timeout: float,
        poll: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Poll until current; raise `SchemaWaitTimeout` after `timeout` seconds."""
        deadline = monotonic() + timeout
        while not await self.refresh():
            if monotonic() >= deadline:
                raise SchemaWaitTimeout(
                    f"schema version {self.version} < required {self.required} after {timeout}s"
                )
            await asyncio.sleep(poll)
        logger.info("schema is current", extra={"version": self.version})

    async def wait_or_exit(
        self,
        *,
        timeout: float,
        poll: float,
        exit_process: Callable[[int], object] = os._exit,
    ) -> None:
        """Background form for the api: wait, and exit non-zero only after the timeout."""
        try:
            await self.wait(timeout=timeout, poll=poll)
        except SchemaWaitTimeout:
            logger.critical("schema not current after the wait; exiting")
            exit_process(1)


class SchemaCheck:
    """The api's readiness check for the schema (healthy once the gate is current)."""

    name = SCHEMA_CHECK_NAME

    def __init__(self) -> None:
        """Start unbound (not ready) until `register_schema_check` binds a gate."""
        self.gate: SchemaGate | None = None

    async def check(self) -> HealthCheckResult:
        """Healthy iff the bound gate has seen a current schema (no database call per probe)."""
        if self.gate is not None and self.gate.current:
            return HealthCheckResult(self.name, healthy=True)
        return HealthCheckResult(self.name, healthy=False, detail=UNHEALTHY_DETAIL)


_SCHEMA_CHECK = SchemaCheck()


def register_schema_check(registry: HealthRegistry, gate: SchemaGate) -> bool:
    """Register the schema check on the template's registry exactly once (invariant 16).

    The check object is a module singleton; every lifespan entry rebinds it to that entry's
    gate. The registry raises `ValueError` for a duplicate name, which means an earlier
    entry already registered it: the idempotent case. Returns True if registered now.
    """
    _SCHEMA_CHECK.gate = gate
    try:
        registry.register(_SCHEMA_CHECK)
    except ValueError:
        return False
    return True
