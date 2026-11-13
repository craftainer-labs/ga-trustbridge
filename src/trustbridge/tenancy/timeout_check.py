"""The transaction time bound check (ADR 0032 "Export", ADR 0034 B8).

The audit export returns only events older than `AUDIT_EXPORT_LAG_SECONDS`, which is sound only if
no audit-writing transaction stays open longer than that. The `after_begin` hook
(`hook._set_transaction_timeout`) sets `transaction_timeout` per transaction (PostgreSQL 17+) from
`TRANSACTION_TIMEOUT_SECONDS`, which the settings validation keeps below the lag. This module reads
what the database *actually* applies, inside a session so the hook has run, and compares it with
the lag, so a hook that is not installed, a lag raised past the bound or a server that ignores the
setting cannot go unnoticed.

The decision is the pure function `evaluate`. With `transaction_timeout` (PostgreSQL 17 and
later) the effective value must be above 0 and below the lag. Without it (earlier servers have no
such parameter) **production fails closed** (PostgreSQL 17+ is required); elsewhere the fallback
applies: `statement_timeout` and `idle_in_transaction_session_timeout`, both above 0 and below the
lag, are accepted with a warning, because a transaction of many short statements can still outlive
the lag. A value of 0 means
"off" in PostgreSQL and is refused. `TimeoutGate`, `TimeoutCheck` and `register_timeout_check`
mirror `role_check`: a failing process reports not ready with the reason; **in production** it
exits non-zero once the shared `SCHEMA_WAIT_SECONDS` budget has passed; **elsewhere** the worker
and the reconciler warn and continue and the api stays not ready with the reason.
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

TIMEOUT_CHECK_NAME = "transaction_timeout"
UNREADABLE_REASON = "the database's transaction timeout settings could not be read"
# The effective values in this session's transaction (so the hook's `SET LOCAL` is visible), in
# milliseconds (the unit of all three settings). A parameter the server lacks gives NULL.
_FACTS = text(
    "SELECT CAST(current_setting('server_version_num') AS integer), "
    "(SELECT CAST(setting AS bigint) FROM pg_settings WHERE name = 'transaction_timeout'), "
    "(SELECT CAST(setting AS bigint) FROM pg_settings WHERE name = 'statement_timeout'), "
    "(SELECT CAST(setting AS bigint) FROM pg_settings "
    "WHERE name = 'idle_in_transaction_session_timeout')"
)


@dataclass(frozen=True, slots=True)
class TimeoutFacts:
    """The effective timeouts of a transaction, in milliseconds (0 is off)."""

    server_version: int  # `server_version_num`, for example 170002
    transaction_timeout_ms: int | None  # None: the server has no such parameter (before 17)
    statement_timeout_ms: int
    idle_in_transaction_ms: int


@dataclass(frozen=True, slots=True)
class TimeoutVerdict:
    """Safe or not, with the reason when not and a warning when only the fallback holds."""

    ok: bool
    reason: str | None = None
    warning: str | None = None


def _bounded(value_ms: int, lag_ms: float) -> bool:
    return 0 < value_ms < lag_ms


def evaluate(
    facts: TimeoutFacts, lag_seconds: float, *, production: bool = False
) -> TimeoutVerdict:
    """Unsafe unless a timeout below `lag_seconds` bounds the transaction.

    Production needs `transaction_timeout` (PostgreSQL 17+); the statement and idle fallback is
    accepted, with a warning, only elsewhere.
    """
    lag_ms = lag_seconds * 1000
    lag = f"AUDIT_EXPORT_LAG_SECONDS ({lag_seconds:g} s)"
    if facts.transaction_timeout_ms is not None:
        if _bounded(facts.transaction_timeout_ms, lag_ms):
            return TimeoutVerdict(ok=True)
        return TimeoutVerdict(
            ok=False,
            reason=(
                f"transaction_timeout is {facts.transaction_timeout_ms} ms (0 is off); "
                f"it must be above 0 and below {lag}"
            ),
        )
    if production:
        return TimeoutVerdict(
            ok=False,
            reason=(
                f"PostgreSQL {facts.server_version} has no transaction_timeout; production "
                "requires PostgreSQL 17 or later (the statement and idle fallback does not bound "
                "a transaction's total time)"
            ),
        )
    if _bounded(facts.statement_timeout_ms, lag_ms) and _bounded(
        facts.idle_in_transaction_ms, lag_ms
    ):
        return TimeoutVerdict(
            ok=True,
            warning=(
                f"PostgreSQL {facts.server_version} has no transaction_timeout (PostgreSQL 17+); "
                "statement_timeout and idle_in_transaction_session_timeout bound each statement "
                "and each idle gap but not a transaction's total, so the export lag guarantee "
                "is weaker; upgrade to PostgreSQL 17 or later"
            ),
        )
    return TimeoutVerdict(
        ok=False,
        reason=(
            f"PostgreSQL {facts.server_version} has no transaction_timeout and "
            f"statement_timeout ({facts.statement_timeout_ms} ms) or "
            f"idle_in_transaction_session_timeout ({facts.idle_in_transaction_ms} ms) is off or "
            f"not below {lag}"
        ),
    )


class TimeoutCheckTimeout(Exception):  # noqa: N818 -- a terminal condition, named for what it says
    """The timeout bound did not become safe within `SCHEMA_WAIT_SECONDS`."""


class TimeoutGate:
    """Reads the effective transaction timeouts and holds the latest verdict."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None,
        lag_seconds: float,
        *,
        mock: bool = False,
        production: bool = False,
    ) -> None:
        """`mock` (MODE=mock) is always ready and never opens a session.

        `production` makes PostgreSQL 17+ with `transaction_timeout` applied mandatory.
        """
        self._sf = session_factory
        self._lag = lag_seconds
        self._production = production
        self._mock = mock
        self.current: bool = mock
        self.reason: str | None = None

    async def read_facts(self) -> TimeoutFacts | None:
        """The effective timeouts; None when the query fails (the gate then fails closed)."""
        if self._sf is None:  # pragma: no cover -- only mock mode passes no factory
            return None
        try:
            async with self._sf() as session:
                row = (await session.execute(_FACTS)).one()
            version, transaction, statement, idle = row
            return TimeoutFacts(
                int(version),
                None if transaction is None else int(transaction),
                int(statement),
                int(idle),
            )
        except Exception:
            logger.warning("database transaction timeouts unreadable", exc_info=True)
            return None

    async def refresh(self) -> bool:
        """Re-read and update `current` and `reason`; a failure or a weaker bound is a warning."""
        if self._mock:
            return True
        facts = await self.read_facts()
        verdict = (
            TimeoutVerdict(ok=False, reason=UNREADABLE_REASON)
            if facts is None
            else evaluate(facts, self._lag, production=self._production)
        )
        self.current, self.reason = verdict.ok, verdict.reason
        if not verdict.ok:
            logger.warning("transaction timeout check failed: %s", verdict.reason)
        elif verdict.warning is not None:
            logger.warning("transaction timeout check: %s", verdict.warning)
        return self.current

    async def wait(
        self,
        *,
        timeout: float,
        poll: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Poll until safe; raise `TimeoutCheckTimeout` after `timeout` seconds."""
        deadline = monotonic() + timeout
        while not await self.refresh():
            if monotonic() >= deadline:
                raise TimeoutCheckTimeout(f"{self.reason} (after {timeout}s)")
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
        except TimeoutCheckTimeout:
            logger.critical("transaction timeout unsafe after SCHEMA_WAIT_SECONDS; exiting")
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

        Elsewhere it keeps re-reading until the bound is safe, reporting not ready meanwhile.
        """
        if production:
            if not await self.startup(production=True, timeout=timeout, poll=poll):
                exit_process(1)
            return
        while not await self.refresh():
            await asyncio.sleep(poll)


class TimeoutCheck:
    """The api's readiness check (healthy once the gate has seen a safe bound)."""

    name = TIMEOUT_CHECK_NAME

    def __init__(self) -> None:
        """Start unbound (not ready) until `register_timeout_check` binds a gate."""
        self.gate: TimeoutGate | None = None

    async def check(self) -> HealthCheckResult:
        """Healthy iff the bound gate is safe; the unhealthy detail is the (static) reason."""
        if self.gate is not None and self.gate.current:
            return HealthCheckResult(self.name, healthy=True)
        reason = self.gate.reason if self.gate is not None else None
        return HealthCheckResult(self.name, healthy=False, detail=reason or UNREADABLE_REASON)


_TIMEOUT_CHECK = TimeoutCheck()


def register_timeout_check(registry: HealthRegistry, gate: TimeoutGate) -> bool:
    """Register the check once; every lifespan entry rebinds it to that entry's gate."""
    _TIMEOUT_CHECK.gate = gate
    try:
        registry.register(_TIMEOUT_CHECK)
    except ValueError:
        return False
    return True
