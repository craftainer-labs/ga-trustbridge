"""Installation syncs report the state they applied to the audit hook (ADR 0032 `tenant.sync`)."""

from collections.abc import Sequence
from uuid import UUID

import pytest

from tests.trustbridge.github.conftest import ScriptedLookup
from trustbridge.adapters.interface import GitHubHost
from trustbridge.github.handlers import EventContext, InstallationHandlers
from trustbridge.worker.ports import MemoryTenants

INSTALLATION = {"id": 4242, "account": {"login": "acme", "id": 5}}


class Audit:
    """A `TenantAudit` that records its calls (or fails when told to)."""

    def __init__(self, tenants: MemoryTenants) -> None:
        """Keep the store to observe the order of effects."""
        self.calls: list[dict[str, object]] = []
        self.repositories_at_call: list[dict[int, str]] = []
        self.fail = False
        self._tenants = tenants

    async def tenant_synced(
        self, *, tenant_id: UUID, state: str, delivery_id: str, sender_id: int | None
    ) -> None:
        """Record the call and the repositories already applied at that moment."""
        if self.fail:
            raise RuntimeError("audit unwritable")
        self.repositories_at_call.append(dict(self._tenants.repositories.get(tenant_id, {})))
        self.calls.append(
            {"tenant": tenant_id, "state": state, "delivery": delivery_id, "sender": sender_id}
        )


class Scheduler:
    """Accepts reconcile requests."""

    async def schedule(self, tenant_id: UUID) -> None:
        """Ignore."""
        del tenant_id


def _projection(name: str) -> GitHubHost:
    raise AssertionError(name)


def _setup() -> tuple[MemoryTenants, Audit, ScriptedLookup, InstallationHandlers]:
    tenants = MemoryTenants(_projection)
    audit, lookup = Audit(tenants), ScriptedLookup()
    return tenants, audit, lookup, InstallationHandlers(tenants, Scheduler(), lookup, audit=audit)


def _ctx(action: str, payload: dict[str, object] | None = None) -> EventContext:
    body: dict[str, object] = {"installation": INSTALLATION, "sender": {"id": 583231}}
    body.update(payload or {})
    return EventContext("github.com", "delivery-1", "installation", action, body)


async def test_created_records_active_after_the_repositories_are_applied() -> None:
    """The event comes last, so a retry after a half-applied sync still ends with an event."""
    tenants, audit, _lookup, handlers = _setup()
    repos: Sequence[dict[str, object]] = [{"id": 10, "full_name": "acme/web"}]
    await handlers.on_installation(_ctx("created", {"repositories": repos}))
    tenant = await tenants.tenant_id_for("github.com", 4242)
    assert audit.calls == [
        {"tenant": tenant, "state": "active", "delivery": "delivery-1", "sender": 583231}
    ]
    assert audit.repositories_at_call == [{10: "acme/web"}]


@pytest.mark.parametrize(
    ("action", "suspended", "state"),
    [("suspend", True, "suspended"), ("unsuspend", False, "active")],
)
async def test_suspend_and_unsuspend_record_the_state_github_reports(
    action: str, suspended: bool, state: str
) -> None:
    """The recorded state is what the API said, not what the event name implies."""
    _tenants, audit, lookup, handlers = _setup()
    lookup.suspended = suspended
    await handlers.on_installation(_ctx(action))
    assert [c["state"] for c in audit.calls] == [state]


async def test_a_known_installation_that_is_gone_records_deleted() -> None:
    """A `deleted` for a tenant we hold: one event with state deleted."""
    tenants, audit, lookup, handlers = _setup()
    await handlers.on_installation(_ctx("created"))
    lookup.gone = True
    await handlers.on_installation(_ctx("deleted"))
    tenant = await tenants.tenant_id_for("github.com", 4242)
    assert [(c["tenant"], c["state"]) for c in audit.calls] == [
        (tenant, "active"),
        (tenant, "deleted"),
    ]


async def test_a_gone_installation_we_never_knew_changes_nothing_and_records_nothing() -> None:
    """No tenant row was touched, so there is nothing to record."""
    _tenants, audit, lookup, handlers = _setup()
    lookup.gone = True
    await handlers.on_installation(_ctx("deleted"))
    assert audit.calls == []


async def test_a_delivery_without_a_numeric_sender_records_no_sender() -> None:
    """A missing or non-numeric `sender.id` is not guessed at."""
    _tenants, audit, _lookup, handlers = _setup()
    await handlers.on_installation(_ctx("created", {"sender": {"id": "583231", "login": "x"}}))
    await handlers.on_installation(_ctx("suspend", {"sender": None}))
    assert [c["sender"] for c in audit.calls] == [None, None]


async def test_a_failing_audit_fails_the_handler_so_the_delivery_is_retried() -> None:
    """The change must not stay without its event: the error reaches the worker."""
    _tenants, audit, _lookup, handlers = _setup()
    audit.fail = True
    with pytest.raises(RuntimeError, match="unwritable"):
        await handlers.on_installation(_ctx("created"))
