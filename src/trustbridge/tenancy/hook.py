"""The SQLAlchemy `after_begin` hook that scopes each transaction to the current tenant."""

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


def install_tenant_hook() -> None:
    """Listen for `after_begin` on every `Session` (the async session proxies to it); idempotent."""
    if not event.contains(Session, "after_begin", _set_tenant_setting):
        event.listen(Session, "after_begin", _set_tenant_setting)
