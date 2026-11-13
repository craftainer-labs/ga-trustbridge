"""The SQLAlchemy `after_begin` hooks: the tenant scope and the transaction time bound.

The tenant hook scopes each transaction to the current tenant (ADR 0031). The timeout hook
bounds how long any transaction of this process may stay open (ADR 0032 "Export", ADR 0034 B8):
the audit export's lag guarantee holds only if no audit-writing transaction outlives the lag.
"""

import math

from sqlalchemy import Connection, event, text
from sqlalchemy.orm import Session, SessionTransaction

from trustbridge.tenancy.context import current_tenant

TENANT_SETTING = "app.tenant_id"
_SET_TENANT = text("SELECT set_config('app.tenant_id', CAST(:tenant_id AS text), true)")


def _set_tenant_setting(
    _session: Session, _transaction: SessionTransaction, connection: Connection
) -> None:
    """Set `app.tenant_id` transaction-locally: the tenant's UUID text, or '' when unset.

    The third argument of `set_config` is `true`, so the value is discarded at commit or
    rollback and never reaches the next user of a pooled connection (PgBouncer transaction mode
    included). Runs on the connection the transaction just began. The only writer of the
    setting; it reads the ContextVar and nothing from a request.
    """
    if connection.dialect.name != "postgresql":
        return
    tenant = current_tenant.get()
    connection.execute(_SET_TENANT, {"tenant_id": "" if tenant is None else str(tenant)})


# `transaction_timeout` exists from PostgreSQL 17. Below it the fallback bounds each statement and
# each idle gap with the same value, which is weaker (see `timeout_check`).
TRANSACTION_TIMEOUT_MIN_VERSION = (17,)
_SET_TRANSACTION_TIMEOUT = text("SELECT set_config('transaction_timeout', :ms, true)")
_SET_FALLBACK_TIMEOUTS = text(
    "SELECT set_config('statement_timeout', :ms, true), "
    "set_config('idle_in_transaction_session_timeout', :ms, true)"
)
_timeout_ms: int | None = None


def configure_transaction_timeout(seconds: float | None) -> None:
    """Set the bound the hook applies (rounded up to whole milliseconds); None turns it off."""
    global _timeout_ms  # noqa: PLW0603 -- one process-wide bound, like the one hook
    _timeout_ms = None if seconds is None else max(1, math.ceil(seconds * 1000))


def _set_transaction_timeout(
    _session: Session, _transaction: SessionTransaction, connection: Connection
) -> None:
    """`SET LOCAL transaction_timeout` (PostgreSQL 17+), or the statement and idle fallback.

    `set_config(..., true)` is `SET LOCAL`: it ends with the transaction, so it never reaches
    the next user of a pooled connection and works behind PgBouncer's transaction mode. The
    server version is the one the dialect read on connect. A no-op when no bound is configured
    or on another dialect. The migrate Job opens no `Session` (it runs Alembic on a bare
    connection as the owner role), so its long DDL transactions are not bounded.
    """
    if _timeout_ms is None or connection.dialect.name != "postgresql":
        return
    version = connection.dialect.server_version_info or ()
    statement = (
        _SET_TRANSACTION_TIMEOUT
        if version >= TRANSACTION_TIMEOUT_MIN_VERSION
        else _SET_FALLBACK_TIMEOUTS
    )
    connection.execute(statement, {"ms": f"{_timeout_ms}ms"})


def install_tenant_hook(transaction_timeout_seconds: float | None = None) -> None:
    """Listen for `after_begin` on every `Session` (the async session proxies to it); idempotent.

    Installs the tenant scope and the transaction time bound; `transaction_timeout_seconds`
    (when given) sets the bound, and a later call replaces it.
    """
    if transaction_timeout_seconds is not None:
        configure_transaction_timeout(transaction_timeout_seconds)
    if not event.contains(Session, "after_begin", _set_tenant_setting):
        event.listen(Session, "after_begin", _set_tenant_setting)
    if not event.contains(Session, "after_begin", _set_transaction_timeout):
        event.listen(Session, "after_begin", _set_transaction_timeout)
