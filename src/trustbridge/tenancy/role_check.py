"""The runtime role safety check (ADR 0031 "Role safety checks", invariant 16).

A superuser, or a role with `BYPASSRLS`, ignores every row-level-security policy. Once any RLS
table is forced (the six tenant-owned tables from release N+1, `audit_events` from its release),
the api, the worker and the reconciler must not run as such a role. This module reads the
process's `current_user` attributes and whether a table is forced, and decides. The tables are
looked up schema-qualified (`APP_SCHEMA`), never through the `search_path`; once the schema gate is
satisfied an expected table that cannot be found (or is not a table) is itself unsafe, never
"not forced", so the check cannot pass a superuser by failing to see the tables.

The decision is the pure function `evaluate`. `RoleGate` mirrors `worker.schema.SchemaGate`
(`current`, `refresh`, `wait`) and `RoleCheck` is the readiness check the api registers next to
the schema check. A process whose role is unsafe reports not ready with the reason; **in
production** it exits non-zero once `SCHEMA_WAIT_SECONDS` (one budget shared with the schema wait,
counted from start-up) have passed. **Outside production** the worker and the reconciler log a
warning and continue, and the api stays not ready with the reason: a dev database is often reached
as a superuser. The check is skipped (ready) while no RLS table is forced yet.
"""

import asyncio
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

from craftainer_observability.health.base import HealthCheckResult
from craftainer_observability.health.registry import HealthRegistry
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

ROLE_CHECK_NAME = "role_safety"
# The six tenant-owned tables plus 0032's `audit_events`: "an RLS table" for this check.
RLS_TABLES = (
    "tenants",
    "tenant_repositories",
    "scope_leases",
    "adapter_resources",
    "adapter_credentials",
    "credential_operations",
    "audit_events",
)
# The schema the app's tables live in (the migrations create them unqualified, in `public`).
APP_SCHEMA = "public"
UNREADABLE_REASON = "the database role's attributes could not be read"
# `to_regclass` is NULL for a name it cannot find; 'r' and 'p' are plain and partitioned tables.
_FACTS = text(
    "SELECT r.rolname, r.rolsuper, r.rolbypassrls, EXISTS ("
    "SELECT 1 FROM unnest(CAST(:tables AS text[])) t JOIN pg_class c "
    "ON c.oid = to_regclass(format('%I.%I', CAST(:schema AS text), t))::oid "
    "WHERE c.relforcerowsecurity AND c.relkind IN ('r', 'p')), ARRAY("
    "SELECT t::text FROM unnest(CAST(:tables AS text[])) t LEFT JOIN pg_class c "
    "ON c.oid = to_regclass(format('%I.%I', CAST(:schema AS text), t))::oid "
    "WHERE c.oid IS NULL OR c.relkind NOT IN ('r', 'p') ORDER BY t) "
    "FROM pg_roles r WHERE r.rolname = current_user"
)


@dataclass(frozen=True, slots=True)
class RoleFacts:
    """What the database says about the connected role and the forced tables."""

    user: str
    superuser: bool
    bypassrls: bool
    forced: bool  # whether any RLS table is forced
    missing: tuple[str, ...] = ()  # expected tables not found as tables in `APP_SCHEMA`


@dataclass(frozen=True, slots=True)
class RoleVerdict:
    """Safe or not, with the reason when not."""

    ok: bool
    reason: str | None = None


def evaluate(facts: RoleFacts) -> RoleVerdict:
    """Unsafe when an RLS table is missing, or one is forced and the role bypasses RLS."""
    if facts.missing:
        return RoleVerdict(
            ok=False,
            reason=(
                f"RLS table(s) {', '.join(facts.missing)} not found in schema {APP_SCHEMA}; "
                "the database role cannot be checked"
            ),
        )
    if not facts.forced:
        return RoleVerdict(ok=True)
    problems = [
        label
        for label, bad in (("a superuser", facts.superuser), ("BYPASSRLS", facts.bypassrls))
        if bad
    ]
    if not problems:
        return RoleVerdict(ok=True)
    return RoleVerdict(
        ok=False,
        reason=(
            f"database role {facts.user} is {' and '.join(problems)}, which ignores "
            "row-level security; run as a role that is neither"
        ),
    )


def budget_left(
    timeout: float, started: float, monotonic: Callable[[], float] = time.monotonic
) -> float:
    """What is left of `timeout` seconds counted from `started` (never negative).

    The schema wait and the role wait share one `SCHEMA_WAIT_SECONDS` budget from start-up.
    """
    return max(0.0, timeout - (monotonic() - started))


class RoleCheckTimeout(Exception):  # noqa: N818 -- a terminal condition, named for what it says
    """The role did not become safe within `SCHEMA_WAIT_SECONDS`."""


class RoleGate:
    """Reads the connected role's attributes and holds the latest verdict."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession] | None, *, mock: bool = False
    ) -> None:
        """`mock` (MODE=mock) is always ready and never opens a session."""
        self._sf = session_factory
        self._mock = mock
        self.current: bool = mock
        self.reason: str | None = None

    async def read_facts(self) -> RoleFacts | None:
        """The role facts; None when the query fails (the gate then fails closed)."""
        if self._sf is None:  # pragma: no cover -- only mock mode passes no factory
            return None
        try:
            async with self._sf() as session:
                params = {"tables": list(RLS_TABLES), "schema": APP_SCHEMA}
                row = (await session.execute(_FACTS, params)).one()
            user, superuser, bypassrls, forced, missing = row
            return RoleFacts(
                str(user), bool(superuser), bool(bypassrls), bool(forced), tuple(map(str, missing))
            )
        except Exception:
            logger.warning("database role attributes unreadable", exc_info=True)
            return None

    async def refresh(self) -> bool:
        """Re-read and update `current` and `reason`; an unsafe role is logged as a warning."""
        if self._mock:
            return True
        facts = await self.read_facts()
        verdict = (
            RoleVerdict(ok=False, reason=UNREADABLE_REASON) if facts is None else evaluate(facts)
        )
        self.current, self.reason = verdict.ok, verdict.reason
        if not verdict.ok:
            logger.warning("role safety check failed: %s", verdict.reason)
        return self.current

    async def wait(
        self,
        *,
        timeout: float,
        poll: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Poll until safe; raise `RoleCheckTimeout` after `timeout` seconds."""
        deadline = monotonic() + timeout
        while not await self.refresh():
            if monotonic() >= deadline:
                raise RoleCheckTimeout(f"{self.reason} (after {timeout}s)")
            await asyncio.sleep(poll)

    async def startup(self, *, production: bool, timeout: float, poll: float) -> bool:
        """The worker/reconciler start-up step; False means exit non-zero.

        Production waits up to `timeout`; elsewhere it checks once, warns and carries on.
        """
        if not production:
            await self.refresh()
            return True
        try:
            await self.wait(timeout=timeout, poll=poll)
        except RoleCheckTimeout:
            logger.critical("database role unsafe after SCHEMA_WAIT_SECONDS; exiting")
            return False
        return True

    async def background(
        self,
        *,
        production: bool,
        timeout: float,
        poll: float,
        exit_process: Callable[[int], object] = os._exit,
    ) -> None:
        """The api's background form: production exits non-zero after the timeout.

        Elsewhere it keeps re-reading until the role is safe, reporting not ready meanwhile.
        """
        if production:
            if not await self.startup(production=True, timeout=timeout, poll=poll):
                exit_process(1)
            return
        while not await self.refresh():
            await asyncio.sleep(poll)


class RoleCheck:
    """The api's readiness check for the role (healthy once the gate has seen a safe role)."""

    name = ROLE_CHECK_NAME

    def __init__(self) -> None:
        """Start unbound (not ready) until `register_role_check` binds a gate."""
        self.gate: RoleGate | None = None

    async def check(self) -> HealthCheckResult:
        """Healthy iff the bound gate is safe; the unhealthy detail is the (static) reason."""
        if self.gate is not None and self.gate.current:
            return HealthCheckResult(self.name, healthy=True)
        reason = self.gate.reason if self.gate is not None else None
        return HealthCheckResult(self.name, healthy=False, detail=reason or UNREADABLE_REASON)


_ROLE_CHECK = RoleCheck()


def register_role_check(registry: HealthRegistry, gate: RoleGate) -> bool:
    """Register the role check once; every lifespan entry rebinds it to that entry's gate."""
    _ROLE_CHECK.gate = gate
    try:
        registry.register(_ROLE_CHECK)
    except ValueError:
        return False
    return True
