"""The single-row and filtered tenant reads and the one-event audit read (scripted session)."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

from tests.trustbridge.admin.helpers import ACME
from trustbridge.admin.access import SqlTenantAccess, tenants_page
from trustbridge.audit.store import get_event
from trustbridge.rbac import Grant, Principal, Role
from trustbridge.tenancy import TenantInfo, current_tenant, get_tenant

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
T1, T2 = UUID(int=1), UUID(int=2)


class Result:
    """A scripted result."""

    def __init__(self, rows: list[Any]) -> None:
        """Hold the rows."""
        self.rows = rows

    def all(self) -> list[Any]:
        """Every row."""
        return self.rows

    def first(self) -> Any:  # noqa: ANN401
        """The first row or None."""
        return self.rows[0] if self.rows else None

    def mappings(self) -> Result:
        """Rows as mappings."""
        return self


class Db:
    """A session factory whose sessions record each statement's parameters."""

    def __init__(self, rows: list[Any]) -> None:
        """Serve `rows` to every statement."""
        self.rows = rows
        self.params: list[dict[str, Any]] = []
        self.contexts: list[UUID | None] = []

    def __call__(self) -> Self:
        """A session."""
        return self

    async def __aenter__(self) -> Self:
        """Open."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close."""

    @asynccontextmanager
    async def begin(self) -> Any:  # noqa: ANN401
        """A transaction."""
        yield

    async def execute(self, _statement: object, params: dict[str, Any]) -> Result:
        """Record and answer."""
        self.params.append(params)
        self.contexts.append(current_tenant.get())
        return Result(self.rows)


def _row(tenant_id: UUID) -> tuple[Any, ...]:
    return (tenant_id, "github.com", 7, "acme", 70, "active", None, False)


async def test_get_tenant_is_one_lookup_by_id_with_no_context() -> None:
    """`tb_get_tenant` by id, in no tenant context; None when absent."""
    db = Db([_row(T1)])
    info = await get_tenant(db, T1)  # type: ignore[arg-type]
    assert info == TenantInfo(T1, "github.com", 7, "acme", 70, "active", None, False)
    assert db.params == [{"id": T1}]
    assert db.contexts == [None]
    assert await get_tenant(Db([]), T2) is None  # type: ignore[arg-type]


async def test_the_rbac_lookup_reads_one_row_too() -> None:
    """`SqlTenantAccess.get_tenant` maps the row, an empty org login to None."""
    db = Db([SimpleRow(ACME.id, "github.com", "")])
    ref = await SqlTenantAccess(db).get_tenant(ACME.id)  # type: ignore[arg-type]
    assert ref is not None
    assert (ref.id, ref.host, ref.org) == (ACME.id, "github.com", None)
    assert await SqlTenantAccess(Db([])).get_tenant(uuid4()) is None  # type: ignore[arg-type]


class SimpleRow:
    """A row with named columns."""

    def __init__(self, id: UUID, host: str, org: str) -> None:  # noqa: A002
        """Set the columns."""
        self.id, self.host, self.org = id, host, org


def _principal(*grants: Grant) -> Principal:
    return Principal("u", "https://idp.example", frozenset(grants))


async def test_a_principal_with_no_grants_reads_nothing_and_asks_no_question() -> None:
    """No grant, no query."""
    db = Db([_row(T1)])
    assert await tenants_page(db, _principal(), after=None, limit=5) == []  # type: ignore[arg-type]
    assert db.params == []


async def test_an_instance_grant_filters_nothing_in_the_database() -> None:
    """`every` is true; the keyset and limit are bound."""
    db = Db([_row(T1), _row(T2)])
    rows = await tenants_page(
        db,  # type: ignore[arg-type]
        _principal(Grant(Role.AUDITOR), Grant(Role.VIEWER, "github.com", "acme")),
        after=T1,
        limit=3,
    )
    assert [r.id for r in rows] == [T1, T2]
    assert db.params == [
        {"after": T1, "every": True, "hosts": ["github.com"], "orgs": ["acme"], "limit": 3}
    ]
    assert db.contexts == [None]


async def test_tenant_grants_filter_by_host_and_org_pairs() -> None:
    """Without an instance grant the pairs (sorted, distinct) are bound."""
    db = Db([])
    await tenants_page(
        db,  # type: ignore[arg-type]
        _principal(
            Grant(Role.VIEWER, "b.example", "z"),
            Grant(Role.OPERATOR, "github.com", "acme"),
            Grant(Role.AUDITOR, "github.com", "acme"),
        ),
        after=None,
        limit=1,
    )
    assert db.params == [
        {
            "after": None,
            "every": False,
            "hosts": ["b.example", "github.com"],
            "orgs": ["z", "acme"],
            "limit": 1,
        }
    ]


async def test_get_event_maps_a_row_and_none_when_absent() -> None:
    """The one-event read returns a record or None."""
    event_id = uuid4()
    row = {
        "id": event_id,
        "seq": 3,
        "occurred_at": NOW,
        "recorded_at": NOW,
        "tenant_id": None,
        "actor_type": "system",
        "actor_sub": "worker",
        "actor_iss": None,
        "on_behalf_of": None,
        "source_ip": None,
        "action": "tenant.sync",
        "resource_type": "tenant",
        "resource_id": None,
        "outcome": "success",
        "before": None,
        "after": '{"k": 1}',
        "request_id": None,
        "trace_id": None,
        "schema_version": 1,
    }
    db = Db([row])
    record = await get_event(db, event_id)  # type: ignore[arg-type]
    assert record is not None
    assert (record.id, record.seq, record.after) == (event_id, 3, {"k": 1})
    assert db.params == [{"id": event_id}]
    assert await get_event(Db([]), event_id) is None  # type: ignore[arg-type]
