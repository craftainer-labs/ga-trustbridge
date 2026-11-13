"""The database foundation of tenant data deletion against a real Postgres (NG6h-1, ADR 0034).

`tenant_deletions`, `tb_request_tenant_deletion`, `tb_delete_tenant`, the `deleting` flag of the
read functions, the enable refusal, the upsert guard and the `webhook_deliveries` changes, plus the
downgrade refusal. Runs in CI's stack (integration), never in the unit lane. It reuses the scratch
database provisioning of `test_rls_integration` (real Alembic chain, `trustbridge_app` as the
caller) and hands the new definer functions to `trustbridge_owner`, so they run as they will in
production: with row-level security enabled and forced.
"""

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Row, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.trustbridge.tenancy.test_rls_integration import (
    INSERTS,
    OWNER_ROLE,
    ROOT,
    Db,
    _provision,
)

pytestmark = pytest.mark.integration

MIGRATION = "9c2e5a7f1d84"
PREVIOUS = "b7d3f5a19c68"
NEW_FUNCTIONS = (
    "tb_get_tenant(uuid)",
    "tb_set_tenant_disabled(uuid, text, text, boolean)",
    "tb_request_tenant_deletion(uuid, text, bigint, text, text, text, boolean, timestamptz)",
    "tb_delete_tenant(uuid)",
)
REQUEST = "SELECT * FROM tb_request_tenant_deletion(:t, :host, :inst, :org, :sub, :iss, :platform)"
REQUEST_NOT_BEFORE = (
    "SELECT * FROM tb_request_tenant_deletion("
    ":t, :host, :inst, :org, :sub, :iss, :platform, :not_before)"
)
SET_DISABLED = "SELECT * FROM tb_set_tenant_disabled(:t, 'admin-1', 'hold', :disable)"
OPEN_STATES = ("requested", "draining", "platform", "github", "database")
TERMINAL_STATES = ("done", "failed", "cancelled")


@pytest.fixture(scope="module")
async def db() -> AsyncIterator[Db]:
    """The shipped schema with RLS enabled and forced and the new functions owned by the owner."""
    async for provisioned in _provision(enforce=True):
        async with provisioned.admin.begin() as conn:
            for function in NEW_FUNCTIONS:
                await conn.execute(text(f"ALTER FUNCTION {function} OWNER TO {OWNER_ROLE}"))
        yield provisioned


class Tenant:
    """A freshly inserted tenant and the values its deletion request must echo."""

    def __init__(self, tenant_id: UUID, host: str, installation: int, org: str) -> None:
        """Keep the tenant's identity."""
        self.id, self.host, self.installation, self.org = tenant_id, host, installation, org

    def request(self, *, org: str | None = None, platform: bool = False) -> dict[str, Any]:
        """The parameters of `tb_request_tenant_deletion` for this tenant."""
        return {
            "t": self.id,
            "host": self.host,
            "inst": self.installation,
            "org": self.org if org is None else org,
            "sub": "admin-1",
            "iss": "https://idp.example",
            "platform": platform,
        }


async def _tenant(db: Db, *, disabled: bool = True) -> Tenant:
    host, installation, org = f"del-{uuid4().hex[:8]}", 1000 + uuid4().int % 100000, "acme-org"
    async with db.admin.begin() as conn:
        tenant_id = (
            await conn.execute(
                text(
                    "INSERT INTO tenants (host, installation_id, org, org_id, state"  # noqa: S608 -- constants only
                    + (
                        ", admin_disabled_at, admin_disabled_by, admin_disable_reason"
                        if disabled
                        else ""
                    )
                    + ") VALUES (:h, :i, :o, 1, 'active'"
                    + (", now(), 'admin-1', 'hold'" if disabled else "")
                    + ") RETURNING id"
                ),
                {"h": host, "i": installation, "o": org},
            )
        ).scalar_one()
    return Tenant(tenant_id, host, installation, org)


async def _app_rows(db: Db, sql: str, params: dict[str, Any]) -> list[Row[Any]]:
    async with db.app() as session, session.begin():
        return list((await session.execute(text(sql), params)).all())


async def _admin(db: Db, sql: str, params: dict[str, Any] | None = None) -> None:
    async with db.admin.begin() as conn:
        await conn.execute(text(sql), params or {})


async def _admin_scalar(db: Db, sql: str, params: dict[str, Any] | None = None) -> Any:  # noqa: ANN401
    async with db.admin.begin() as conn:
        return (await conn.execute(text(sql), params or {})).scalar_one()


async def _request(db: Db, tenant: Tenant, **kwargs: Any) -> Row[Any]:  # noqa: ANN401
    return (await _app_rows(db, REQUEST, tenant.request(**kwargs)))[0]


async def _set_state(db: Db, deletion: UUID, state: str) -> None:
    await _admin(
        db, "UPDATE tenant_deletions SET state = :s WHERE id = :d", {"s": state, "d": deletion}
    )


# --- the request function ------------------------------------------------------------------


async def test_a_request_is_refused_for_every_unmet_precondition_and_changes_nothing(
    db: Db,
) -> None:  # ADR 0034 inv. 1
    """Unknown tenant, not disabled, and a wrong echo of host, installation or login."""
    assert (await _request(db, await _tenant(db, disabled=False))).outcome == "not_disabled"
    tenant = await _tenant(db)
    assert (await _app_rows(db, REQUEST, {**tenant.request(), "t": uuid4()}))[
        0
    ].outcome == "not_found"
    for wrong in ({"org": "other-org"}, {"host": "other.host"}, {"inst": tenant.installation + 1}):
        row = (await _app_rows(db, REQUEST, {**tenant.request(), **wrong}))[0]
        assert row.outcome == "confirmation_mismatch"
    assert (
        await _admin_scalar(
            db, "SELECT count(*) FROM tenant_deletions WHERE tenant_id = :t", {"t": tenant.id}
        )
        == 0
    )


async def test_a_request_writes_the_record_without_the_login_and_repeats_return_it(
    db: Db,
) -> None:  # inv. 2, 3, 14
    """Created once, the same record afterwards, and the organisation login is never stored."""
    tenant = await _tenant(db)
    created = await _request(db, tenant, platform=True)
    assert (created.outcome, created.state, created.delete_platform_resources) == (
        "created",
        "requested",
        True,
    )
    again = await _request(db, tenant)  # a repeat returns the open record, flags and all
    assert (again.outcome, again.deletion_id, again.delete_platform_resources) == (
        "existing",
        created.deletion_id,
        True,
    )
    stored = await _admin_scalar(
        db, "SELECT t::text FROM tenant_deletions t WHERE tenant_id = :t", {"t": tenant.id}
    )
    assert tenant.org not in stored
    async with db.admin.begin() as conn:
        record = (
            await conn.execute(
                text("SELECT * FROM tenant_deletions WHERE id = :d"), {"d": created.deletion_id}
            )
        ).one()
    assert (record.host, record.installation_id) == (tenant.host, tenant.installation)
    assert (record.requested_by_sub, record.requested_by_iss) == ("admin-1", "https://idp.example")
    # a new request after a terminal record is a second record
    await _set_state(db, created.deletion_id, "failed")
    second = await _request(db, tenant)
    assert (second.outcome, second.deletion_id == created.deletion_id) == ("created", False)


async def test_a_replay_needs_the_confirmation_too(db: Db) -> None:
    """The open record is returned only to a caller that echoes the tenant correctly."""
    tenant = await _tenant(db)
    created = await _request(db, tenant)
    for wrong in ({"org": "other-org"}, {"host": "other.host"}, {"inst": tenant.installation + 1}):
        row = (await _app_rows(db, REQUEST, {**tenant.request(), **wrong}))[0]
        assert (row.outcome, row.deletion_id) == ("confirmation_mismatch", None)
    assert (await _request(db, tenant)).deletion_id == created.deletion_id


async def test_the_grace_window_is_stored_and_defaults_to_no_delay(db: Db) -> None:
    """`not_before` round-trips through the request function; omitted, it is NULL."""
    stored = "SELECT not_before FROM tenant_deletions WHERE id = :d"
    plain = await _request(db, await _tenant(db))
    assert await _admin_scalar(db, stored, {"d": plain.deletion_id}) is None
    tenant = await _tenant(db)
    moment = datetime(2031, 5, 6, 7, 8, 9, tzinfo=UTC)
    rows = await _app_rows(db, REQUEST_NOT_BEFORE, {**tenant.request(), "not_before": moment})
    assert rows[0].outcome == "created"
    assert await _admin_scalar(db, stored, {"d": rows[0].deletion_id}) == moment


async def test_a_request_rejects_malformed_requester_values(db: Db) -> None:
    """A blank or oversized `sub` is refused with 22023 and writes nothing."""
    tenant = await _tenant(db)
    for bad in ("", "x" * 256):
        with pytest.raises(DBAPIError, match="invalid deletion request"):
            await _app_rows(db, REQUEST, {**tenant.request(), "sub": bad})
    assert (
        await _admin_scalar(
            db, "SELECT count(*) FROM tenant_deletions WHERE tenant_id = :t", {"t": tenant.id}
        )
        == 0
    )


# --- deleting and enable -------------------------------------------------------------------


async def test_deleting_is_true_exactly_while_a_non_terminal_deletion_exists(db: Db) -> None:
    """`tb_list_tenants` and `tb_get_tenant` report it; the other tenants stay false."""
    tenant = await _tenant(db)

    async def flags() -> tuple[bool, bool, set[bool]]:
        listed = {
            r.id: r.deleting for r in await _app_rows(db, "SELECT * FROM tb_list_tenants()", {})
        }
        got = (await _app_rows(db, "SELECT deleting FROM tb_get_tenant(:t)", {"t": tenant.id}))[0]
        others = {listed[db.a], listed[db.b]}  # the seeded tenants never have a deletion
        return listed[tenant.id], got.deleting, others

    assert await flags() == (False, False, {False})
    deletion = (await _request(db, tenant)).deletion_id
    for state in OPEN_STATES:
        await _set_state(db, deletion, state)
        assert await flags() == (True, True, {False})
    for state in TERMINAL_STATES:
        await _set_state(db, deletion, state)
        assert await flags() == (False, False, {False})


async def test_enable_is_refused_while_a_deletion_is_open_and_allowed_after(db: Db) -> None:
    """SQLSTATE 55000 for every open state; disable stays idempotent; a terminal one frees it."""
    tenant = await _tenant(db)
    deletion = (await _request(db, tenant)).deletion_id
    for state in OPEN_STATES:
        await _set_state(db, deletion, state)
        with pytest.raises(DBAPIError, match="deletion in progress"):
            await _app_rows(db, SET_DISABLED, {"t": tenant.id, "disable": False})
        repeat = (await _app_rows(db, SET_DISABLED, {"t": tenant.id, "disable": True}))[0]
        assert repeat.after_disabled_by == "admin-1"  # unchanged by the repeat
    await _set_state(db, deletion, "cancelled")
    row = (await _app_rows(db, SET_DISABLED, {"t": tenant.id, "disable": False}))[0]
    assert row.after_disabled_at is None


async def test_a_request_and_an_enable_never_both_win(db: Db) -> None:
    """Racing them, the tenant never ends enabled with an open deletion (the shared row lock)."""
    for _ in range(12):
        tenant = await _tenant(db)
        results = await asyncio.gather(
            _app_rows(db, SET_DISABLED, {"t": tenant.id, "disable": False}),
            _app_rows(db, REQUEST, tenant.request()),
            return_exceptions=True,
        )
        async with db.admin.begin() as conn:
            state = (
                await conn.execute(
                    text(
                        "SELECT t.admin_disabled_at IS NOT NULL AS disabled, EXISTS ("
                        "SELECT 1 FROM tenant_deletions d WHERE d.tenant_id = t.id "
                        "AND d.state NOT IN ('done', 'failed', 'cancelled')) AS opened "
                        "FROM tenants t WHERE t.id = :t"
                    ),
                    {"t": tenant.id},
                )
            ).one()
        disabled, opened = state.disabled, state.opened
        assert not (opened and not disabled), results
        enable, request = results
        if isinstance(request, BaseException):
            raise request
        if isinstance(enable, BaseException):  # the request won: the enable was refused
            assert opened
        else:  # the enable won: the request saw a tenant that is not disabled
            assert request[0].outcome == "not_disabled"
            assert not opened


async def test_a_pending_request_holds_the_lock_an_enable_waits_for(db: Db) -> None:
    """An enable issued while a request is uncommitted waits, then is refused."""
    tenant = await _tenant(db)
    async with db.app() as holder, holder.begin():
        created = (await holder.execute(text(REQUEST), tenant.request())).one()
        assert created.outcome == "created"
        waiting = asyncio.create_task(
            _app_rows(db, SET_DISABLED, {"t": tenant.id, "disable": False})
        )
        await asyncio.sleep(0.5)
        assert not waiting.done()
    with pytest.raises(DBAPIError, match="deletion in progress"):
        await waiting


async def test_at_most_one_open_deletion_per_tenant_and_states_are_checked(db: Db) -> None:
    """The partial unique index and the state CHECK hold even for a direct writer."""
    tenant = await _tenant(db)
    insert = (
        "INSERT INTO tenant_deletions (tenant_id, host, installation_id, requested_by_sub, "
        "requested_by_iss, state) VALUES (:t, 'h', 1, 's', 'i', :s)"
    )
    await _admin(db, insert, {"t": tenant.id, "s": "requested"})
    with pytest.raises(IntegrityError):
        await _admin(db, insert, {"t": tenant.id, "s": "draining"})
    await _admin(db, insert, {"t": tenant.id, "s": "done"})  # terminal rows are not limited
    await _admin(db, insert, {"t": tenant.id, "s": "cancelled"})
    with pytest.raises(IntegrityError):
        await _admin(db, insert, {"t": uuid4(), "s": "bogus"})


async def test_the_app_role_can_only_read_deletion_records(db: Db) -> None:
    """SELECT is granted; every write goes through a definer function (a compliance record)."""
    tenant = await _tenant(db)
    deletion = (await _request(db, tenant)).deletion_id  # the definer function may write
    assert len(
        await _app_rows(db, "SELECT id FROM tenant_deletions WHERE id = :d", {"d": deletion})
    )
    writes = (
        "INSERT INTO tenant_deletions (tenant_id, host, installation_id, requested_by_sub, "
        "requested_by_iss, state) VALUES (:t, 'h', 1, 's', 'i', 'database')",
        "UPDATE tenant_deletions SET state = 'cancelled' WHERE id = :d",
        "DELETE FROM tenant_deletions WHERE id = :d",
    )
    for write in writes:
        with pytest.raises(DBAPIError, match="permission denied"):
            await _app_rows(db, write, {"t": tenant.id, "d": deletion})
    assert (
        await _admin_scalar(
            db, "SELECT state FROM tenant_deletions WHERE tenant_id = :t", {"t": tenant.id}
        )
        == "requested"
    )
    assert (
        await _admin_scalar(
            db, "SELECT count(*) FROM tenant_deletions WHERE id = :d", {"d": deletion}
        )
        == 1
    )


# --- tb_delete_tenant ----------------------------------------------------------------------

DELETE = "SELECT tb_delete_tenant(:t)"


async def _exists(db: Db, tenant: Tenant) -> bool:
    return bool(
        await _admin_scalar(
            db, "SELECT EXISTS (SELECT 1 FROM tenants WHERE id = :t)", {"t": tenant.id}
        )
    )


async def test_tb_delete_tenant_refuses_unless_disabled_and_in_the_database_step(
    db: Db,
) -> None:  # 0031 inv. 6
    """Each unmet condition raises 55000 and the tenant row stays."""
    enabled = await _tenant(db, disabled=False)
    with pytest.raises(DBAPIError, match="not admin-disabled"):
        await _app_rows(db, DELETE, {"t": enabled.id})
    tenant = await _tenant(db)
    with pytest.raises(DBAPIError, match="no deletion in its database step"):
        await _app_rows(db, DELETE, {"t": tenant.id})
    deletion = (await _request(db, tenant)).deletion_id
    for state in (*(s for s in OPEN_STATES if s != "database"), *TERMINAL_STATES):
        await _set_state(db, deletion, state)
        with pytest.raises(DBAPIError, match="no deletion in its database step"):
            await _app_rows(db, DELETE, {"t": tenant.id})
    assert await _exists(db, enabled)
    assert await _exists(db, tenant)


async def test_tb_delete_tenant_refuses_during_the_grace_window(db: Db) -> None:
    """A future `not_before` blocks it even in the database step; once it has passed, it runs."""
    tenant = await _tenant(db)
    deletion = (await _request(db, tenant)).deletion_id
    await _set_state(db, deletion, "database")
    await _admin(
        db,
        "UPDATE tenant_deletions SET not_before = now() + interval '1 hour' WHERE id = :d",
        {"d": deletion},
    )
    with pytest.raises(DBAPIError, match="grace window"):
        await _app_rows(db, DELETE, {"t": tenant.id})
    assert await _exists(db, tenant)
    await _admin(
        db,
        "UPDATE tenant_deletions SET not_before = now() - interval '1 second' WHERE id = :d",
        {"d": deletion},
    )
    await _app_rows(db, DELETE, {"t": tenant.id})
    assert not await _exists(db, tenant)


async def test_a_delete_and_an_upsert_never_recreate_the_tenant(db: Db) -> None:
    """The advisory lock serialises them: an upsert queued behind a delete sees the tombstone."""
    upsert = "SELECT tb_upsert_tenant(:h, :i, 'acme-org', 1, 'active')"
    tenant = await _tenant(db)
    deletion = (await _request(db, tenant)).deletion_id
    await _set_state(db, deletion, "database")
    args = {"h": tenant.host, "i": tenant.installation}
    async with db.app() as deleter, deleter.begin():
        await deleter.execute(text(DELETE), {"t": tenant.id})  # uncommitted: lock held
        racing = asyncio.create_task(_app_rows(db, upsert, args))
        await asyncio.sleep(0.5)
        assert not racing.done()  # without the lock it would have found the row and returned
    assert (await racing)[0][0] is None
    count = "SELECT count(*) FROM tenants WHERE host = :h AND installation_id = :i"
    assert await _admin_scalar(db, count, args) == 0
    # the reverse order: an upsert holding the lock makes the delete wait, then it still deletes
    other = await _tenant(db)
    other_deletion = (await _request(db, other)).deletion_id
    await _set_state(db, other_deletion, "database")
    async with db.app() as upserter, upserter.begin():
        await upserter.execute(text(upsert), {"h": other.host, "i": other.installation})
        waiting = asyncio.create_task(_app_rows(db, DELETE, {"t": other.id}))
        await asyncio.sleep(0.5)
        assert not waiting.done()
    await waiting
    assert not await _exists(db, other)


async def test_a_request_waits_for_the_installation_advisory_lock(db: Db) -> None:
    """A deletion request queues behind an upsert holding the installation lock."""
    tenant = await _tenant(db)
    key = f"tb_tenant:{tenant.host}:{tenant.installation}"
    async with db.app() as holder, holder.begin():
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": key}
        )
        waiting = asyncio.create_task(_request(db, tenant))
        await asyncio.sleep(0.5)
        assert not waiting.done()
    assert (await waiting).outcome == "created"


async def test_tb_delete_tenant_refuses_while_any_tenant_keyed_row_remains(db: Db) -> None:
    """One row in any of the five tables blocks it, and removing it lets the delete through."""
    tenant = await _tenant(db)
    deletion = (await _request(db, tenant)).deletion_id
    await _set_state(db, deletion, "database")
    for table, insert in INSERTS.items():
        await _admin(db, insert, {"t": tenant.id, "k": "leftover", "n": 1})
        with pytest.raises(DBAPIError, match=f"still has rows in: {table}"):
            await _app_rows(db, DELETE, {"t": tenant.id})
        assert await _exists(db, tenant)
        await _admin(db, f"DELETE FROM {table} WHERE tenant_id = :t", {"t": tenant.id})  # noqa: S608 -- a constant name
    await _app_rows(db, DELETE, {"t": tenant.id})
    assert not await _exists(db, tenant)
    await _app_rows(db, DELETE, {"t": tenant.id})  # a resumed step is a no-op


async def test_tb_delete_tenant_removes_only_the_tenant_row(db: Db) -> None:
    """Other tenants, the deletion record, audit events and deliveries are untouched."""
    tenant = await _tenant(db)
    deletion = (await _request(db, tenant)).deletion_id
    await _set_state(db, deletion, "database")
    await _admin(
        db,
        "INSERT INTO webhook_deliveries (host, delivery_id, event, headers, status, tenant_id) "
        "VALUES (:h, 'd1', 'push', CAST('{}' AS jsonb), 'pending', :t)",
        {"h": tenant.host, "t": tenant.id},
    )
    await _app_rows(db, DELETE, {"t": tenant.id})
    assert (
        await _admin_scalar(
            db, "SELECT count(*) FROM tenants WHERE id = ANY(:ids)", {"ids": [db.a, db.b]}
        )
        == 2
    )
    assert (
        await _admin_scalar(
            db,
            "SELECT count(*) FROM scope_leases WHERE tenant_id = ANY(:ids)",
            {"ids": [db.a, db.b]},
        )
        == 2
    )
    assert (
        await _admin_scalar(
            db, "SELECT count(*) FROM tenant_deletions WHERE id = :d", {"d": deletion}
        )
        == 1
    )
    assert (
        await _admin_scalar(
            db, "SELECT count(*) FROM webhook_deliveries WHERE tenant_id = :t", {"t": tenant.id}
        )
        == 1
    )


# --- tb_upsert_tenant ----------------------------------------------------------------------


async def test_tb_upsert_tenant_returns_a_deleted_tenant_unchanged(db: Db) -> None:
    """A late event cannot revive it; other states still update."""
    host, inst = f"up-{uuid4().hex[:8]}", 4242
    upsert = "SELECT tb_upsert_tenant(:h, :i, :o, :oi, :s)"
    first = (
        await _app_rows(db, upsert, {"h": host, "i": inst, "o": "one", "oi": 1, "s": "active"})
    )[0][0]
    renamed = (
        await _app_rows(db, upsert, {"h": host, "i": inst, "o": "two", "oi": 2, "s": "suspended"})
    )[0][0]
    assert renamed == first
    async with db.admin.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT org, org_id, state FROM tenants WHERE id = :t"), {"t": first}
            )
        ).one()
        assert (row.org, row.org_id, row.state) == ("two", 2, "suspended")
        await conn.execute(text("UPDATE tenants SET state = 'deleted' WHERE id = :t"), {"t": first})
    again = (
        await _app_rows(db, upsert, {"h": host, "i": inst, "o": "three", "oi": 3, "s": "active"})
    )[0][0]
    assert again == first
    async with db.admin.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT org, org_id, state FROM tenants WHERE id = :t"), {"t": first}
            )
        ).one()
    assert (row.org, row.org_id, row.state) == ("two", 2, "deleted")


async def test_tb_upsert_tenant_treats_a_done_deletion_as_a_tombstone(db: Db) -> None:
    """After `done` no tenant is created for the key; other keys and states are unaffected."""
    upsert = "SELECT tb_upsert_tenant(:h, :i, :o, :oi, 'active')"

    def args(tenant: Tenant, inst: int | None = None) -> dict[str, Any]:
        return {"h": tenant.host, "i": inst or tenant.installation, "o": "x", "oi": 1}

    tenant = await _tenant(db)
    deletion = (await _request(db, tenant)).deletion_id
    for state in (*OPEN_STATES, "failed", "cancelled"):  # none of these is a tombstone
        await _set_state(db, deletion, state)
        assert (await _app_rows(db, upsert, args(tenant)))[0][0] == tenant.id
    await _set_state(db, deletion, "database")
    await _admin(db, "DELETE FROM tenants WHERE id = :t", {"t": tenant.id})
    # the window after tb_delete_tenant and before `done`: the row is gone, so it counts
    assert (await _app_rows(db, upsert, args(tenant)))[0][0] is None
    await _set_state(db, deletion, "done")
    assert (await _app_rows(db, upsert, args(tenant)))[0][0] is None
    count = "SELECT count(*) FROM tenants WHERE host = :h AND installation_id = :i"
    assert await _admin_scalar(db, count, {"h": tenant.host, "i": tenant.installation}) == 0
    fresh = (await _app_rows(db, upsert, args(tenant, tenant.installation + 1)))[0][0]
    assert fresh is not None  # a new installation id of the same host is a new tenant


# --- webhook_deliveries --------------------------------------------------------------------

DELIVERY = (
    "INSERT INTO webhook_deliveries (host, delivery_id, event, headers, status) "
    "VALUES ('h', :d, 'push', CAST('{}' AS jsonb), :s)"
)


async def test_webhook_deliveries_gain_tenant_id_and_the_deleted_status(db: Db) -> None:
    """The previous release's insert still works; `deleted` is allowed, other values are not."""
    await _app_rows(
        db, DELIVERY + " RETURNING 1", {"d": uuid4().hex, "s": "pending"}
    )  # as N-1 does
    await _app_rows(db, DELIVERY + " RETURNING 1", {"d": uuid4().hex, "s": "deleted"})
    with pytest.raises(DBAPIError, match="ck_wd_status"):
        await _app_rows(db, DELIVERY + " RETURNING 1", {"d": uuid4().hex, "s": "bogus"})
    constraints = await _admin_scalar(
        db,
        "SELECT array_agg(conname ORDER BY conname) FROM pg_constraint "
        "WHERE conrelid = 'webhook_deliveries'::regclass AND contype = 'c' "
        "AND conname LIKE 'ck_wd_status%'",
    )
    assert constraints == ["ck_wd_status"]
    assert await _admin_scalar(
        db, "SELECT convalidated FROM pg_constraint WHERE conname = 'ck_wd_status'"
    )
    index = await _admin_scalar(
        db, "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_wd_tenant'"
    )
    assert "(tenant_id)" in index
    assert "WHERE (tenant_id IS NOT NULL)" in index
    assert await _admin_scalar(
        db,
        "SELECT is_nullable = 'YES' FROM information_schema.columns "
        "WHERE table_name = 'webhook_deliveries' AND column_name = 'tenant_id'",
    )


# --- downgrade -----------------------------------------------------------------------------


async def _alembic(database: str, *args: str) -> subprocess.CompletedProcess[str]:
    return await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env={**os.environ, "POSTGRES_DB": database},
        capture_output=True,
        text=True,
        check=False,
    )


async def test_downgrade_is_refused_while_a_deletion_record_exists_and_round_trips() -> None:
    """The compliance record blocks it; without one the previous bodies come back."""
    async for scratch in _provision(enforce=False):
        tenant = await _tenant(scratch)
        deletion = (await _request(scratch, tenant)).deletion_id
        refused = await _alembic(scratch.name, "downgrade", PREVIOUS)
        assert refused.returncode != 0
        assert "cannot downgrade" in refused.stderr
        assert await _admin_scalar(scratch, "SELECT count(*) FROM tenant_deletions") == 1
        await _admin(scratch, "DELETE FROM tenant_deletions WHERE id = :d", {"d": deletion})
        # a `deleted` delivery cannot be represented by the previous CHECK: refused up front
        await _admin(scratch, DELIVERY, {"d": "gone", "s": "deleted"})
        deliveries = await _alembic(scratch.name, "downgrade", PREVIOUS)
        assert deliveries.returncode != 0
        assert "status deleted" in deliveries.stderr
        assert await _admin_scalar(scratch, "SELECT to_regclass('tenant_deletions') IS NOT NULL")
        await _admin(scratch, "DELETE FROM webhook_deliveries WHERE delivery_id = 'gone'")
        done = await _alembic(scratch.name, "downgrade", PREVIOUS)
        assert done.returncode == 0, done.stderr[-2000:]
        assert await _admin_scalar(scratch, "SELECT to_regclass('tenant_deletions') IS NULL")
        assert await _admin_scalar(
            scratch, "SELECT to_regprocedure('tb_delete_tenant(uuid)') IS NULL"
        )
        assert await _admin_scalar(
            scratch,
            "SELECT NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE "
            "table_name = 'webhook_deliveries' AND column_name = 'tenant_id')",
        )
        assert await _admin_scalar(
            scratch,
            "SELECT pg_get_functiondef('tb_list_tenants()'::regprocedure) NOT LIKE "
            "'%tenant_deletions%'",
        )
        again = await _alembic(scratch.name, "upgrade", "head")
        assert again.returncode == 0, again.stderr[-2000:]
        assert await _admin_scalar(scratch, "SELECT to_regclass('tenant_deletions') IS NOT NULL")
