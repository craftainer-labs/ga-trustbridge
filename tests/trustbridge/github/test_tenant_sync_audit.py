"""Installation syncs report the state they applied to the audit hook (ADR 0032 `tenant.sync`)."""

from collections.abc import Sequence
from typing import NamedTuple
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


@pytest.mark.parametrize("reason", ["tombstone", "deleting", "deleted"])
async def test_an_event_for_a_deleted_installation_is_dropped_without_a_retry(
    reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The store refuses it: no error, no audit event, no repository change, an info log."""
    tenants, audit, _lookup, handlers = _setup()
    await handlers.on_installation(_ctx("created"))
    audit.calls.clear()
    if reason == "deleted":
        await tenants.set_installation_state("github.com", 4242, "deleted")
    else:
        getattr(tenants, "tombstones" if reason == "tombstone" else "deleting").add(
            ("github.com", 4242)
        )
    caplog.set_level("INFO")
    repos: Sequence[dict[str, object]] = [{"id": 11, "full_name": "acme/api"}]
    await handlers.on_installation(_ctx("created", {"repositories": repos}))
    assert audit.calls == []
    tenant = await tenants.tenant_id_for("github.com", 4242)
    assert tenant is not None
    assert 11 not in tenants.repositories.get(tenant, {})
    assert f"github.com installation 4242 ({reason})" in caplog.text


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


async def test_a_sender_id_outside_the_audit_range_records_no_sender() -> None:
    """An id the audit model would refuse (0, negative, above 2^63-1) is a null, not a failure."""
    _tenants, audit, _lookup, handlers = _setup()
    for bad in (0, -5, 2**63):
        await handlers.on_installation(_ctx("created", {"sender": {"id": bad}}))
    await handlers.on_installation(_ctx("created", {"sender": {"id": 2**63 - 1}}))
    assert [c["sender"] for c in audit.calls] == [None, None, None, 2**63 - 1]


class Counting(Scheduler):
    """A scheduler that remembers what it was asked to schedule."""

    def __init__(self) -> None:
        """Start empty."""
        self.scheduled: list[UUID] = []

    async def schedule(self, tenant_id: UUID) -> None:
        """Remember the request."""
        self.scheduled.append(tenant_id)


class Refused(NamedTuple):
    """A tenant that was active and then became deleted, deleting or tombstoned."""

    tenants: MemoryTenants
    audit: Audit
    scheduler: Counting
    lookup: ScriptedLookup
    handlers: InstallationHandlers
    dropped: list[tuple[str, int]]


async def _refused(reason: str) -> Refused:
    """An active tenant with one repository that then becomes `reason`."""
    tenants, lookup, scheduler = MemoryTenants(_projection), ScriptedLookup(), Counting()
    audit = Audit(tenants)
    dropped: list[tuple[str, int]] = []
    handlers = InstallationHandlers(
        tenants,
        scheduler,
        lookup,
        on_installation_changed=lambda host, inst: dropped.append((host, inst)),
        audit=audit,
    )
    repos: Sequence[dict[str, object]] = [{"id": 11, "full_name": "acme/api"}]
    await handlers.on_installation(_ctx("created", {"repositories": repos}))
    if reason == "deleted":
        await tenants.set_installation_state("github.com", 4242, "deleted")
    else:
        getattr(tenants, "tombstones" if reason == "tombstone" else "deleting").add(
            ("github.com", 4242)
        )
    audit.calls.clear()
    scheduler.scheduled.clear()
    dropped.clear()
    return Refused(tenants, audit, scheduler, lookup, handlers, dropped)


@pytest.mark.parametrize("reason", ["tombstone", "deleting", "deleted"])
async def test_repository_events_for_a_refused_tenant_change_and_schedule_nothing(
    reason: str,
) -> None:
    """ADR 0034: no repository write and no reconcile for a deleted, deleting or tombstoned one."""
    tenants, _audit, scheduler, _lookup, handlers, dropped = await _refused(reason)
    tenant = await tenants.tenant_id_for("github.com", 4242)
    assert tenant is not None
    known = dict(tenants.repositories[tenant])
    payload: dict[str, object] = {
        "installation": INSTALLATION,
        "repositories_added": [{"id": 12, "full_name": "acme/new"}],
        "repositories_removed": [{"id": 11}],
        "repository": {"id": 11, "full_name": "acme/renamed"},
    }
    await handlers.on_installation_repositories(
        EventContext("github.com", "d2", "installation_repositories", "added", payload)
    )
    for action in ("renamed", "deleted", "archived"):
        await handlers.on_repository(
            EventContext("github.com", "d3", "repository", action, payload)
        )
    await handlers.on_installation(_ctx("new_permissions_accepted"))
    assert tenants.repositories[tenant] == known
    assert scheduler.scheduled == []
    assert dropped == [("github.com", 4242)] * 6  # new_permissions_accepted notifies first too


@pytest.mark.parametrize("reason", ["tombstone", "deleting"])
async def test_a_gone_installation_of_a_refused_tenant_is_dropped_without_an_event(
    reason: str,
) -> None:
    """The state setter refuses it: no error to retry, no audit event, the tokens are dropped."""
    tenants, audit, _scheduler, lookup, handlers, dropped = await _refused(reason)
    lookup.gone = True
    await handlers.on_installation(_ctx("deleted"))
    assert audit.calls == []
    assert dropped == [("github.com", 4242)]
    tenant = await tenants.tenant_id_for("github.com", 4242)
    assert tenant is not None
    assert tenants._rows[tenant]["state"] == "active"


async def test_a_repeated_gone_event_for_a_deleted_installation_is_recorded_again() -> None:
    """`deleted` on `deleted` is not refused, so a retry after a failed audit write still ends."""
    tenants, audit, _scheduler, lookup, handlers, _dropped = await _refused("deleted")
    lookup.gone = True
    await handlers.on_installation(_ctx("deleted"))
    tenant = await tenants.tenant_id_for("github.com", 4242)
    assert [(c["tenant"], c["state"]) for c in audit.calls] == [(tenant, "deleted")]
