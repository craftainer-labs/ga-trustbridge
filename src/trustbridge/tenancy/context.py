"""The tenant of the running task (ADR 0031 "The tenant context")."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID

current_tenant: ContextVar[UUID | None] = ContextVar("current_tenant", default=None)


@contextmanager
def tenant_context(tenant_id: UUID | None) -> Iterator[None]:
    """Run the body with `tenant_id` as the current tenant, restoring the previous value after.

    The only writer of `current_tenant` (token-based, so it is reset on exit including when
    the body raises). `None` means "explicitly no tenant". The value must come from a resolved
    tenant (the worker's resolution step or the admin API's RBAC check), never straight from a
    request field. The `after_begin` hook (`trustbridge.tenancy.hook`) reads it when a
    transaction begins, so open the session inside the context.
    """
    token = current_tenant.set(tenant_id)
    try:
        yield
    finally:
        current_tenant.reset(token)
