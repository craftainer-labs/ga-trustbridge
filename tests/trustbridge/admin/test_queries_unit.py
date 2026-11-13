"""The tenant-scoped and delivery reads over a scripted session (the SQL runs in the integration
suite): parameters, keyset cursors, JSON decoding and the payload gate."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from trustbridge.admin import queries
from trustbridge.admin.cursor import decode_cursor, encode_cursor
from trustbridge.admin.problems import AdminProblem

T = UUID(int=7)
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


class Rows:
    """What `session.execute` returns."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        """Hold the rows."""
        self.rows = rows

    def mappings(self) -> Rows:
        """Rows as mappings."""
        return self

    def __iter__(self) -> Any:  # noqa: ANN401
        """Iterate the rows."""
        return iter(self.rows)

    def first(self) -> dict[str, Any] | None:
        """The first row, if any."""
        return self.rows[0] if self.rows else None

    rowcount = 1


class Session:
    """Records the bound parameters and serves one scripted result."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        """Serve `rows`."""
        self.rows = rows
        self.params: dict[str, Any] = {}

    async def execute(self, _statement: object, params: dict[str, Any]) -> Rows:
        """Record the parameters and answer."""
        self.params = params
        return Rows(self.rows)


def _repo(n: int) -> dict[str, Any]:
    return {"tenant_id": T, "repository_id": n, "full_name": f"o/r{n}"}


async def test_repositories_page_and_cursor() -> None:
    """One extra row decides `next_cursor`; the cursor is the last id sent."""
    session = Session([_repo(1), _repo(2), _repo(3)])
    items, cursor = await queries.repositories(session, T, limit=2, cursor=None)  # type: ignore[arg-type]
    assert [i.repository_id for i in items] == [1, 2]
    assert session.params == {"tenant_id": T, "after": 0, "limit": 3}
    assert cursor is not None
    assert decode_cursor(cursor) == (2, "2")
    last, none = await queries.repositories(Session([_repo(3)]), T, limit=2, cursor=cursor)  # type: ignore[arg-type]
    assert [i.repository_id for i in last] == [3]
    assert none is None
    resumed = Session([])
    await queries.repositories(resumed, T, limit=2, cursor=cursor)  # type: ignore[arg-type]
    assert resumed.params["after"] == 2


async def test_a_text_cursor_is_not_a_repository_position() -> None:
    """A cursor of the wrong kind is a 400."""
    with pytest.raises(AdminProblem) as caught:
        await queries.repositories(Session([]), T, limit=2, cursor=encode_cursor("a", "b"))  # type: ignore[arg-type]
    assert caught.value.status_code == 400


async def test_resources_decode_text_json_and_resume_by_adapter_and_key() -> None:
    """A driver that returns JSONB as text is decoded; the cursor is `(adapter, key)`."""
    row = {
        "tenant_id": T,
        "adapter": "ghcr",
        "resource_key": "k1",
        "state": json.dumps({"name": "n"}),
        "updated_at": NOW,
    }
    second = {**row, "resource_key": "k2", "state": {"name": "m"}}
    session = Session([row, second])
    items, cursor = await queries.resources(session, T, limit=1, cursor=None)  # type: ignore[arg-type]
    assert items[0].state == {"name": "n", "ref": {}}
    assert session.params["adapter"] == session.params["key"] == ""
    assert cursor is not None
    assert decode_cursor(cursor) == ("ghcr", "k1")
    again = Session([])
    await queries.resources(again, T, limit=1, cursor=cursor)  # type: ignore[arg-type]
    assert (again.params["adapter"], again.params["key"]) == ("ghcr", "k1")


async def test_an_integer_cursor_is_not_a_text_position() -> None:
    """The (adapter, key) cursor needs a text first part."""
    with pytest.raises(AdminProblem):
        await queries.resources(Session([]), T, limit=1, cursor=encode_cursor(5, "x"))  # type: ignore[arg-type]


async def test_credentials_hold_names_and_state() -> None:
    """All three JSONB columns come back as objects and lists."""
    row = {
        "tenant_id": T,
        "adapter": "jfrog",
        "scope_key": "abc",
        "scope": json.dumps({"repository_id": 1}),
        "kind": None,
        "state": {"current": None, "previous": None},
        "outputs": json.dumps([{"kind": "secret", "name": "X"}]).encode(),
        "updated_at": NOW,
    }
    items, cursor = await queries.credentials(Session([row]), T, limit=5, cursor=None)  # type: ignore[arg-type]
    assert cursor is None
    assert items[0].scope == {
        "repository_id": 1,
        "repository_owner_id": None,
        "environment": None,
        "permission": None,
    }
    assert items[0].outputs == [
        {"kind": "secret", "name": "X", "repository_id": None, "environment": None}
    ]
    assert items[0].state == {"current": None, "previous": None}


SECRET = "ghp_" + "A1b2C3d4" * 6  # opaque-run shaped
PLANTED = {"token": SECRET, "password": "hunter2hunter2", "private_key": SECRET}


def _everything_shown(model: BaseModel) -> str:
    return json.dumps(model.model_dump(mode="json"))


async def test_credential_reads_drop_unknown_keys_and_scrub_values() -> None:
    """A planted secret-looking key or value in any JSONB column never reaches the response."""
    slot = {
        "generation": 2,
        "name": "tb-cred",
        "ref": {
            "id": "42",
            **PLANTED,
            "note": f"token={SECRET}",
            "not an identifier": "x",
            "n": 5,
            "k" * 40: "x",
        },
        "delivered": True,
        "retire_after": None,
        **PLANTED,
    }
    row = {
        "tenant_id": T,
        "adapter": "jfrog",
        "scope_key": "abc",
        "scope": {"repository_id": 1, "environment": None, "permission": "read", **PLANTED},
        "kind": None,
        "state": {"current": slot, "previous": None, **PLANTED},
        "outputs": [{"kind": "secret", "name": "X", **PLANTED}, "not-a-mapping"],
        "updated_at": NOW,
    }
    items, _ = await queries.credentials(Session([row]), T, limit=5, cursor=None)  # type: ignore[arg-type]
    odd_session = Session([{**row, "outputs": {"kind": "secret", **PLANTED}}])
    odd, _ = await queries.credentials(odd_session, T, limit=5, cursor=None)  # type: ignore[arg-type]
    assert odd[0].outputs == []
    shown = _everything_shown(items[0])
    assert SECRET not in shown
    assert "hunter2" not in shown
    assert "password" not in shown
    assert "private_key" not in shown
    assert items[0].state["current"]["ref"] == {"id": "42", "note": "token=[REDACTED]"}
    assert items[0].state["current"]["generation"] == 2
    assert [o["name"] for o in items[0].outputs] == ["X"]


async def test_resource_state_drops_unknown_keys() -> None:
    """A resource's state shows its name and reference only."""
    row = {
        "tenant_id": T,
        "adapter": "harbor",
        "resource_key": "p",
        "state": {"name": "n", "ref": {"id": "1", "secret": SECRET}, **PLANTED},
        "updated_at": NOW,
    }
    items, _ = await queries.resources(Session([row]), T, limit=5, cursor=None)  # type: ignore[arg-type]
    assert items[0].state == {"name": "n", "ref": {"id": "1"}}
    assert SECRET not in _everything_shown(items[0])


async def test_operation_reads_scrub_text_and_base_state() -> None:
    """`error` and `manual_action` are scrubbed; `base_state` is allow-listed like `state`."""
    row = {
        **_operation(0),
        "error": f"platform said 401 Authorization: Bearer {SECRET} and token={SECRET}",
        "manual_action": f"paste {SECRET} into the console",
        "base_state": {"current": {"generation": 1, "name": "n", "ref": {}, **PLANTED}},
    }
    items, _ = await queries.operations(Session([row]), T, limit=5, cursor=None)  # type: ignore[arg-type]
    shown = _everything_shown(items[0])
    assert SECRET not in shown
    assert "hunter2" not in shown
    assert items[0].base_state is not None
    assert items[0].base_state["current"]["generation"] == 1
    assert items[0].error is not None
    assert "[REDACTED]" in items[0].error


async def test_a_delivery_error_is_scrubbed_and_bounded() -> None:
    """A viewer sees `last_error`, scrubbed and truncated the way audit free text is."""
    long = "x " * 400
    row = _delivery(0, last_error=f"relay failed token={SECRET} {long}")
    items, _ = await queries.deliveries(Session([row]), limit=5, cursor=None)  # type: ignore[arg-type]
    error = items[0].last_error
    assert error is not None
    assert SECRET not in error
    assert len(error) <= 256
    assert error.endswith("[truncated]")
    one = Session([{**_delivery(0, last_error=f"token={SECRET}"), "body": None}])
    detail = await queries.delivery(one, "github.com", "d0", with_payload=False)  # type: ignore[arg-type]
    assert detail is not None
    assert SECRET not in (detail.last_error or "")


def _operation(offset: int) -> dict[str, Any]:
    return {
        "id": UUID(int=offset + 1),
        "tenant_id": T,
        "adapter": "ghcr",
        "scope_key": "s",
        "kind": "issue",
        "status": "finished",
        "oidc_offered": False,
        "base_state": None,
        "base_kind": None,
        "attempts": 0,
        "error": None,
        "manual_action": None,
        "created_at": NOW - timedelta(minutes=offset),
        "updated_at": NOW,
    }


async def test_operations_are_newest_first_and_resume_after_the_last_pair() -> None:
    """The cursor is `(created_at, id)`; the first page binds no time."""
    session = Session([_operation(0), _operation(1)])
    items, cursor = await queries.operations(session, T, limit=1, cursor=None)  # type: ignore[arg-type]
    assert len(items) == 1
    assert session.params["since"] is None
    assert cursor is not None
    resumed = Session([])
    await queries.operations(resumed, T, limit=1, cursor=cursor)  # type: ignore[arg-type]
    assert resumed.params["since"] == NOW
    assert resumed.params["id"] == UUID(int=1)


@pytest.mark.parametrize(
    "cursor",
    [
        encode_cursor(5, "x"),  # not text
        encode_cursor("not a time", str(uuid4())),
        encode_cursor("2026-11-04T12:00:00", str(uuid4())),  # no zone
        encode_cursor(NOW.isoformat(), "not-a-uuid"),
    ],
)
async def test_a_bad_operations_cursor_is_a_400(cursor: str) -> None:
    """Position kind, timestamp syntax, zone and id are all checked."""
    with pytest.raises(AdminProblem) as caught:
        await queries.operations(Session([]), T, limit=1, cursor=cursor)  # type: ignore[arg-type]
    assert caught.value.status_code == 400


def _delivery(offset: int, **extra: Any) -> dict[str, Any]:  # noqa: ANN401
    return {
        "host": "github.com",
        "delivery_id": f"d{offset}",
        "event": "push",
        "status": "pending",
        "attempts": 0,
        "received_at": NOW - timedelta(minutes=offset),
        "next_attempt_at": None,
        "last_relayed_at": None,
        "processed_at": None,
        "dead_at": None,
        "last_error": None,
        "body_sha256": "h",
        "has_body": True,
        **extra,
    }


async def test_deliveries_resume_after_the_last_host_and_id() -> None:
    """Newest first; the ident is `host/delivery_id`."""
    session = Session([_delivery(0), _delivery(1)])
    items, cursor = await queries.deliveries(session, limit=1, cursor=None)  # type: ignore[arg-type]
    assert [i.delivery_id for i in items] == ["d0"]
    assert session.params["since"] is None
    assert cursor is not None
    assert decode_cursor(cursor)[1] == "github.com/d0"
    resumed = Session([])
    await queries.deliveries(resumed, limit=1, cursor=cursor)  # type: ignore[arg-type]
    assert (resumed.params["host"], resumed.params["delivery_id"]) == ("github.com", "d0")
    assert resumed.params["since"] == NOW


async def test_a_delivery_cursor_without_a_host_and_id_is_a_400() -> None:
    """The ident must split into a host and an id."""
    with pytest.raises(AdminProblem):
        await queries.deliveries(
            Session([]),  # type: ignore[arg-type]
            limit=1,
            cursor=encode_cursor(NOW.isoformat(), "no-slash"),
        )


async def test_one_delivery_returns_the_payload_only_when_asked() -> None:
    """`with_body` is bound false for a viewer, and the payload is then never set."""
    body = json.dumps({"action": "opened"}).encode()
    viewer = Session([_delivery(0, body=body)])
    seen = await queries.delivery(viewer, "github.com", "d0", with_payload=False)  # type: ignore[arg-type]
    assert seen is not None
    assert seen.payload is None
    assert viewer.params["with_body"] is False
    operator = Session([_delivery(0, body=body)])
    got = await queries.delivery(operator, "github.com", "d0", with_payload=True)  # type: ignore[arg-type]
    assert got is not None
    assert got.payload == {"action": "opened"}
    assert operator.params["with_body"] is True


@pytest.mark.parametrize("body", [None, b"not json", b"\xff\xfe"])
async def test_a_missing_or_non_json_body_is_a_null_payload(body: bytes | None) -> None:
    """A purged body or a body that is not JSON gives null, never an error."""
    got = await queries.delivery(
        Session([_delivery(0, body=body, has_body=body is not None)]),  # type: ignore[arg-type]
        "github.com",
        "d0",
        with_payload=True,
    )
    assert got is not None
    assert got.payload is None


async def test_an_unknown_delivery_is_none() -> None:
    """No row, no delivery."""
    assert await queries.delivery(Session([]), "github.com", "x", with_payload=True) is None  # type: ignore[arg-type]


# ---- requeue (NG6e-3) --------------------------------------------------------------------


class Script:
    """A session that answers the lock query from one row and records every statement."""

    def __init__(self, row: dict[str, Any] | None, *, updated: int = 1) -> None:
        """Serve `row` for the lock query; the UPDATE affects `updated` rows."""
        self.row = row
        self.updated = updated
        self.statements: list[str] = []

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Rows:  # noqa: ANN401
        """Record the statement text and answer."""
        del params
        self.statements.append(str(statement))
        rows = Rows([self.row] if self.row is not None else [])
        rows.rowcount = self.updated
        return rows


def _row(status: str, *, has_body: bool = True) -> dict[str, Any]:
    return {
        "status": status,
        "attempts": 8,
        "dead_at": NOW,
        "last_error": "boom",
        "next_attempt_at": None,
        "has_body": has_body,
    }


async def test_requeue_resets_a_dead_delivery_with_a_body() -> None:
    """Locked, then updated; before is the dead state and after the reset one."""
    session = Script(_row("dead"))
    done = await queries.requeue_delivery(session, "github.com", "d1")  # type: ignore[arg-type]
    assert done is not None
    assert done.changed is True
    assert (done.before["status"], done.before["attempts"]) == ("dead", 8)
    assert done.after == {
        "status": "pending",
        "attempts": 0,
        "dead_at": None,
        "last_error": None,
        "next_attempt_at": None,
    }
    assert "next_attempt_at" in done.before
    assert "FOR UPDATE" in session.statements[0]
    assert session.statements[1].startswith("UPDATE webhook_deliveries")


async def test_requeue_that_updates_no_row_is_a_conflict() -> None:
    """The store's conditional UPDATE found nothing to reset: a 409, never a false success."""
    session = Script(_row("dead"), updated=0)
    with pytest.raises(AdminProblem) as caught:
        await queries.requeue_delivery(session, "github.com", "d1")  # type: ignore[arg-type]
    assert caught.value.status_code == 409


async def test_requeue_of_a_pending_delivery_changes_nothing() -> None:
    """Only the lock query runs."""
    session = Script(_row("pending"))
    done = await queries.requeue_delivery(session, "github.com", "d1")  # type: ignore[arg-type]
    assert done is not None
    assert done.changed is False
    assert done.before == done.after
    assert len(session.statements) == 1


async def test_requeue_of_an_unknown_delivery_is_none() -> None:
    """No row, no update."""
    session = Script(None)
    assert await queries.requeue_delivery(session, "github.com", "d1") is None  # type: ignore[arg-type]
    assert len(session.statements) == 1


@pytest.mark.parametrize(("status", "has_body"), [("processed", True), ("dead", False)])
async def test_requeue_refuses_what_cannot_be_handled_again(status: str, has_body: bool) -> None:
    """A processed delivery, or a dead one whose body was purged, is a 409 and is not updated."""
    session = Script(_row(status, has_body=has_body))
    with pytest.raises(AdminProblem) as caught:
        await queries.requeue_delivery(session, "github.com", "d1")  # type: ignore[arg-type]
    assert (caught.value.status_code, caught.value.kind) == (409, "conflict")
    assert len(session.statements) == 1
