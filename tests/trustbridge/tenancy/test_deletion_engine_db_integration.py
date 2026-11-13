"""The deletion engine's database half against a real Postgres (NG6h-4a-1, ADR 0034).

The claim, the fenced transitions, retry and failure, cancel, finish (the tombstone) and the fence
check of `tb_delete_tenant` (migration `5d7a2c8e1b94`). Runs in CI's stack (integration), never in
the unit lane. It has its own scratch database: a claim takes any open deletion, so it must not see
the rows of other test modules. The caller is `trustbridge_app`, as in production.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Row, text
from sqlalchemy.exc import DBAPIError

from tests.trustbridge.tenancy.test_deletion_db_integration import (
    NEW_FUNCTIONS,
    PREVIOUS,
    Tenant,
    _admin,
    _admin_rows,
    _admin_scalar,
    _alembic,
    _app_rows,
    _request,
    _tenant,
)
from tests.trustbridge.tenancy.test_rls_integration import OWNER_ROLE, Db, _provision
from trustbridge.worker.ports import SqlTenants

pytestmark = pytest.mark.integration

REVISION = "5d7a2c8e1b94"
BEFORE = "3b8d6f2a4c17"
CLAIM = "SELECT * FROM tb_claim_tenant_deletion(:h, :ttl)"
ADVANCE = (
    "SELECT tb_advance_tenant_deletion(:d, :h, :f, :to, "
    "CAST(:cursor AS jsonb), CAST(:counts AS jsonb), CAST(:skipped AS jsonb))"
)
RETRY = "SELECT tb_retry_tenant_deletion(:d, :h, :f, :e, :delay)"
RENEW = "SELECT tb_renew_tenant_deletion(:d, :h, :f, :ttl)"
RELEASE = "SELECT tb_release_tenant_deletion(:d, :h, :f)"
FAIL = "SELECT tb_fail_tenant_deletion(:d, :h, :f, :r)"
OPERATOR_FAIL = "SELECT tb_operator_fail_tenant_deletion(:d, :a, :r)"
CANCEL = "SELECT tb_cancel_tenant_deletion(:d)"
FINISH = "SELECT tb_finish_tenant_deletion(:d, :h, :f, CAST(:counts AS jsonb), NULL)"
DELETE = "SELECT tb_delete_tenant(:t, :h, :f)"
LOST = "deletion lease lost"
HARDENING = "6f3b9d2e4a85"  # pg_temp on every definer, retry reset on progress, delivery backstop
CHAIN = ("requested", "draining", "platform", "github", "database")


@pytest.fixture(scope="module")
async def db() -> AsyncIterator[Db]:
    """A scratch database of its own, with RLS enforced and the functions owned by the owner."""
    async for provisioned in _provision(enforce=True):
        async with provisioned.admin.begin() as conn:
            for function in NEW_FUNCTIONS:
                await conn.execute(text(f"ALTER FUNCTION {function} OWNER TO {OWNER_ROLE}"))
        yield provisioned


async def _drain(db: Db) -> None:
    """Make every open deletion terminal, so a claim only sees the test's own rows."""
    await _admin(
        db,
        "UPDATE tenant_deletions SET state = 'cancelled', holder = NULL, expires_at = NULL "
        "WHERE state NOT IN ('done', 'failed', 'cancelled')",
    )


async def _open(db: Db, *, not_before: str | None = None) -> tuple[Tenant, UUID]:
    """A disabled tenant with a `requested` deletion."""
    tenant = await _tenant(db)
    deletion = (await _request(db, tenant)).deletion_id
    if not_before is not None:
        await _admin(
            db,
            f"UPDATE tenant_deletions SET not_before = now() + interval '{not_before}' "  # noqa: S608 -- a test constant
            "WHERE id = :d",
            {"d": deletion},
        )
    return tenant, deletion


async def _claim(db: Db, holder: str = "w1", ttl: int = 60) -> Row[Any] | None:
    rows = await _app_rows(db, CLAIM, {"h": holder, "ttl": ttl})
    return rows[0] if rows else None


def _lease(deletion: UUID, row: Row[Any]) -> dict[str, Any]:
    return {"d": deletion, "h": row.holder, "f": row.fence}


async def _advance(
    db: Db, deletion: UUID, row: Row[Any], to: str, **data: str | None
) -> list[Row[Any]]:
    params = {"to": to, "cursor": None, "counts": None, "skipped": None, **data}
    return await _app_rows(db, ADVANCE, {**_lease(deletion, row), **params})


async def _walk(db: Db, deletion: UUID, row: Row[Any], to: str) -> None:
    for step in CHAIN[1 : CHAIN.index(to) + 1]:
        await _advance(db, deletion, row, step)


async def _record(db: Db, deletion: UUID) -> Row[Any]:
    return (await _admin_rows(db, "SELECT * FROM tenant_deletions WHERE id = :d", {"d": deletion}))[
        0
    ]


async def _in_database_step(db: Db) -> tuple[Tenant, UUID, Row[Any]]:
    await _drain(db)
    tenant, deletion = await _open(db)
    row = await _claim(db)
    assert row is not None
    await _walk(db, deletion, row, "database")
    return tenant, deletion, row


# --- claim ---------------------------------------------------------------------------------


async def test_a_claim_takes_the_lease_and_bumps_the_fence(db: Db) -> None:
    """A claim takes the lease and bumps the fence."""
    await _drain(db)
    _, deletion = await _open(db)
    first = await _claim(db, "w1", 60)
    assert first is not None
    assert (first.id, first.holder, first.fence, first.state) == (deletion, "w1", 1, "requested")
    assert await _claim(db, "w2") is None  # the lease is live
    await _admin(db, "UPDATE tenant_deletions SET expires_at = now() - interval '1 second'")
    second = await _claim(db, "w2")
    assert second is not None
    assert (second.id, second.holder, second.fence) == (deletion, "w2", 2)


async def test_a_claim_skips_terminal_future_not_before_and_backed_off_rows(db: Db) -> None:
    """A claim skips terminal future not before and backed off rows."""
    await _drain(db)
    _, waiting = await _open(db, not_before="1 hour")
    _, backoff = await _open(db)
    await _admin(
        db,
        "UPDATE tenant_deletions SET retry_at = now() + interval '1 hour' WHERE id = :d",
        {"d": backoff},
    )
    _, done = await _open(db)
    await _admin(db, "UPDATE tenant_deletions SET state = 'failed' WHERE id = :d", {"d": done})
    assert await _claim(db) is None
    await _admin(
        db,
        "UPDATE tenant_deletions SET not_before = now() - interval '1 second' WHERE id = :d",
        {"d": waiting},
    )
    claimed = await _claim(db)
    assert claimed is not None
    assert claimed.id == waiting


async def test_concurrent_claims_never_take_the_same_deletion(db: Db) -> None:  # ADR inv. 5
    """Eight claimers over three open deletions: each deletion is won exactly once."""
    await _drain(db)
    ids = {(await _open(db))[1] for _ in range(3)}
    rows = await asyncio.gather(*(_claim(db, f"w{n}") for n in range(8)))
    won = [r for r in rows if r is not None]
    assert len(won) == 3
    assert {r.id for r in won} == ids


async def test_the_app_role_may_execute_but_not_write_and_the_search_path_is_pinned(
    db: Db,
) -> None:
    """The app role may execute but not write and the search path is pinned."""
    for function in NEW_FUNCTIONS:
        path = "pg_catalog, public, pg_temp"  # the older definers gain pg_temp in HARDENING
        row = (
            await _admin_rows(
                db,
                "SELECT has_function_privilege('trustbridge_app', :f, 'EXECUTE'), "
                "has_function_privilege('public', :f, 'EXECUTE'), "
                "(SELECT p.prosecdef AND p.proconfig @> ARRAY[CAST(:p AS text)] "
                "FROM pg_proc p WHERE p.oid = CAST(:f AS regprocedure))",
                {"f": function, "p": f"search_path={path}"},
            )
        )[0]
        assert tuple(row) == (True, False, True), function
    with pytest.raises(DBAPIError, match="permission denied"):
        await _app_rows(db, "UPDATE tenant_deletions SET state = 'done'", {})


# --- fencing -------------------------------------------------------------------------------


async def test_a_stale_holder_cannot_commit_any_transition(db: Db) -> None:  # ADR inv. 5
    """After a re-claim the old holder's every guarded call is refused."""
    await _drain(db)
    _, deletion = await _open(db)
    old = await _claim(db, "old")
    assert old is not None
    await _admin(db, "UPDATE tenant_deletions SET expires_at = now() - interval '1 second'")
    new = await _claim(db, "new")
    assert new is not None
    calls: dict[str, dict[str, Any]] = {
        ADVANCE: {"to": "draining", "cursor": None, "counts": None, "skipped": None},
        RETRY: {"e": "x", "delay": 1},
        RENEW: {"ttl": 10},
        RELEASE: {},
        FAIL: {"r": "x"},
        FINISH: {"counts": None},
    }
    for sql, extra in calls.items():
        with pytest.raises(DBAPIError, match=LOST):
            await _app_rows(db, sql, {**_lease(deletion, old), **extra})
    # the same holder with the old fence, and the new holder with the old fence
    with pytest.raises(DBAPIError, match=LOST):
        await _app_rows(db, RELEASE, {"d": deletion, "h": new.holder, "f": old.fence})
    assert (await _record(db, deletion)).state == "requested"
    await _app_rows(db, RELEASE, _lease(deletion, new))


async def test_an_expired_lease_cannot_commit_even_for_its_own_holder(db: Db) -> None:
    """An expired lease cannot commit even for its own holder."""
    await _drain(db)
    _, deletion = await _open(db)
    row = await _claim(db)
    assert row is not None
    await _admin(db, "UPDATE tenant_deletions SET expires_at = now() - interval '1 second'")
    with pytest.raises(DBAPIError, match=LOST):
        await _advance(db, deletion, row, "draining")
    with pytest.raises(DBAPIError, match="invalid lease arguments"):
        await _app_rows(db, RELEASE, {"d": deletion, "h": None, "f": None})


async def test_renew_extends_the_live_lease(db: Db) -> None:
    """Renew extends the live lease."""
    await _drain(db)
    _, deletion = await _open(db)
    row = await _claim(db, ttl=5)
    assert row is not None
    renewed = (await _app_rows(db, RENEW, {**_lease(deletion, row), "ttl": 600}))[0][0]
    assert renewed - row.expires_at > timedelta(seconds=500)
    shorter = (await _app_rows(db, RENEW, {**_lease(deletion, row), "ttl": 1}))[0][0]
    assert shorter == renewed  # a renew never shortens the lease
    with pytest.raises(DBAPIError, match="invalid ttl"):
        await _app_rows(db, RENEW, {**_lease(deletion, row), "ttl": 0})


async def test_tb_delete_tenant_checks_the_holder_the_fence_and_the_live_lease(db: Db) -> None:
    """Tb delete tenant checks the holder the fence and the live lease."""
    tenant, _, row = await _in_database_step(db)
    wrong = (
        {"h": "other", "f": row.fence},
        {"h": row.holder, "f": row.fence + 1},
        {"h": None, "f": row.fence},
        {"h": row.holder, "f": None},
    )
    for args in wrong:
        with pytest.raises(DBAPIError, match=LOST):
            await _app_rows(db, DELETE, {"t": tenant.id, **args})
    await _admin(db, "UPDATE tenant_deletions SET expires_at = now() - interval '1 second'")
    with pytest.raises(DBAPIError, match=LOST):
        await _app_rows(db, DELETE, {"t": tenant.id, "h": row.holder, "f": row.fence})
    assert await _admin_scalar(db, "SELECT count(*) FROM tenants WHERE id = :t", {"t": tenant.id})


async def test_a_live_lease_lets_tb_delete_tenant_through_then_finish_makes_the_tombstone(
    db: Db,
) -> None:
    """A live lease lets tb delete tenant through then finish makes the tombstone."""
    tenant, deletion, row = await _in_database_step(db)
    lease = {**_lease(deletion, row)}
    with pytest.raises(DBAPIError, match="tenant row still exists"):
        await _app_rows(db, FINISH, {**lease, "counts": None})
    await _app_rows(db, DELETE, {"t": tenant.id, "h": row.holder, "f": row.fence})
    await _app_rows(db, FINISH, {**lease, "counts": '{"tenants": 1}'})
    record = await _record(db, deletion)
    assert (record.state, record.holder, record.expires_at, record.counts) == (
        "done",
        None,
        None,
        {"tenants": 1},
    )
    refusal = None
    try:
        await _app_rows(
            db,
            "SELECT tb_upsert_tenant(:h, :i, 'o', 1, 'active')",
            {"h": tenant.host, "i": tenant.installation},
        )
    except DBAPIError as error:
        mapped = SqlTenants.deleted_error(tenant.host, tenant.installation, error)
        refusal = mapped.reason if mapped else None
    assert refusal == "tombstone"
    assert await _claim(db) is None  # a done deletion is never claimed again
    with pytest.raises(DBAPIError, match="deletion is terminal"):
        await _app_rows(db, FINISH, {**lease, "counts": None})


async def test_tb_delete_tenant_refuses_while_a_webhook_delivery_is_not_cleared(db: Db) -> None:
    """An uncleared `webhook_deliveries` row blocks the delete; a `deleted` one does not."""
    tenant, _, row = await _in_database_step(db)
    delivery = (
        "INSERT INTO webhook_deliveries (host, delivery_id, event, headers, status, tenant_id) "
        "VALUES (:h, :d, 'push', CAST('{}' AS jsonb), :s, :t)"
    )
    params = {"h": tenant.host, "d": "wd-1", "t": tenant.id}
    await _admin(db, delivery, {**params, "s": "pending"})
    lease = {"t": tenant.id, "h": row.holder, "f": row.fence}
    with pytest.raises(DBAPIError, match="tenant still has rows in: webhook_deliveries"):
        await _app_rows(db, DELETE, lease)
    assert await _admin_scalar(db, "SELECT count(*) FROM tenants WHERE id = :t", {"t": tenant.id})
    await _admin(
        db,
        "UPDATE webhook_deliveries SET status = 'deleted', body = NULL "
        "WHERE host = :h AND delivery_id = 'wd-1'",
        {"h": tenant.host},
    )
    await _admin(
        db, delivery, {**params, "d": "wd-2", "s": "pending", "t": uuid4()}
    )  # another tenant
    await _app_rows(db, DELETE, lease)
    assert not await _admin_scalar(
        db, "SELECT count(*) FROM tenants WHERE id = :t", {"t": tenant.id}
    )


# --- advance -------------------------------------------------------------------------------


async def test_advance_walks_the_chain_in_order_and_keeps_or_resets_the_cursor(db: Db) -> None:
    """Advance walks the chain in order and keeps or resets the cursor."""
    await _drain(db)
    _, deletion = await _open(db)
    row = await _claim(db)
    assert row is not None
    await _advance(db, deletion, row, "draining", cursor='{"n": 1}', counts='{"a": 1}')
    assert (await _record(db, deletion)).step_cursor == {"n": 1}
    await _advance(db, deletion, row, "draining", cursor='{"n": 2}', skipped='[{"k": 1}]')
    record = await _record(db, deletion)
    assert (record.step_cursor, record.counts, record.skipped) == (
        {"n": 2},
        {"a": 1},
        [{"k": 1}],
    )
    await _advance(db, deletion, row, "draining")  # same state, no cursor: unchanged
    assert (await _record(db, deletion)).step_cursor == {"n": 2}
    for step in CHAIN[2:]:
        await _advance(db, deletion, row, step)
        record = await _record(db, deletion)
        assert (record.state, record.step_cursor) == (step, {})  # a new step starts empty


async def test_advance_refuses_skips_reversals_unknown_states_and_bad_data(db: Db) -> None:
    """Advance refuses skips reversals unknown states and bad data."""
    await _drain(db)
    _, deletion = await _open(db)
    row = await _claim(db)
    assert row is not None
    for target in ("platform", "database", "done", "failed", "cancelled", "bogus"):
        with pytest.raises(DBAPIError, match="invalid deletion transition"):
            await _advance(db, deletion, row, target)
    with pytest.raises(DBAPIError, match="invalid deletion transition"):
        await _app_rows(
            db,
            ADVANCE,
            {**_lease(deletion, row), "to": None, "cursor": None, "counts": None, "skipped": None},
        )
    await _advance(db, deletion, row, "draining")
    with pytest.raises(DBAPIError, match="invalid deletion transition"):
        await _advance(db, deletion, row, "requested")
    for bad in ({"cursor": "[]"}, {"counts": "1"}, {"skipped": "{}"}):
        with pytest.raises(DBAPIError, match="invalid step data"):
            await _advance(db, deletion, row, "draining", **bad)
    assert (await _record(db, deletion)).state == "draining"


async def test_leaving_requested_needs_the_grace_window_over_and_a_disabled_tenant(
    db: Db,
) -> None:
    """Leaving requested needs the grace window over and a disabled tenant."""
    await _drain(db)
    _, deletion = await _open(db)
    row = await _claim(db)
    assert row is not None
    await _admin(
        db,
        "UPDATE tenant_deletions SET not_before = now() + interval '1 hour' WHERE id = :d",
        {"d": deletion},
    )
    with pytest.raises(DBAPIError, match="grace window"):
        await _advance(db, deletion, row, "draining")
    await _admin(db, "UPDATE tenant_deletions SET not_before = NULL WHERE id = :d", {"d": deletion})
    await _admin(
        db,
        "UPDATE tenants SET admin_disabled_at = NULL, admin_disabled_by = NULL, "
        "admin_disable_reason = NULL WHERE id = "
        "(SELECT tenant_id FROM tenant_deletions WHERE id = :d)",
        {"d": deletion},
    )
    with pytest.raises(DBAPIError, match="not admin-disabled"):
        await _advance(db, deletion, row, "draining")
    assert (await _record(db, deletion)).state == "requested"


async def test_leaving_requested_is_refused_when_the_tenant_row_is_missing(db: Db) -> None:
    """The admin-disabled guard fails closed when the tenants row is gone."""
    await _drain(db)
    tenant, deletion = await _open(db)
    row = await _claim(db)
    assert row is not None
    await _admin(db, "DELETE FROM tenants WHERE id = :t", {"t": tenant.id})
    with pytest.raises(DBAPIError, match="the tenant row is gone"):
        await _advance(db, deletion, row, "draining")
    assert (await _record(db, deletion)).state == "requested"


async def test_a_same_state_save_with_an_unchanged_cursor_keeps_the_retry_state(db: Db) -> None:
    """No cursor, or the cursor it already has, is no progress: a poison step keeps counting."""
    await _drain(db)
    _, deletion = await _open(db)
    row = await _claim(db)
    assert row is not None
    await _advance(db, deletion, row, "draining", cursor='{"page": 2}')
    await _app_rows(db, RETRY, {**_lease(deletion, row), "e": "boom", "delay": 0})
    again = await _claim(db)
    assert again is not None
    for save in ({}, {"cursor": '{"page": 2}'}, {"cursor": '{"page":   2}', "counts": '{"n": 1}'}):
        await _advance(db, deletion, again, "draining", **save)
        record = await _record(db, deletion)
        assert (record.attempts, record.last_error, record.state) == (1, "boom", "draining")
        assert record.retry_at is not None
        assert record.step_cursor == {"page": 2}
    await _advance(db, deletion, again, "platform")  # a step change always clears
    record = await _record(db, deletion)
    assert (record.attempts, record.last_error, record.retry_at) == (0, None, None)


async def test_a_same_state_save_with_a_changed_cursor_resets_the_retry_state(db: Db) -> None:
    """A cursor that moved is real progress: attempts, retry_at and last_error are cleared."""
    await _drain(db)
    _, deletion = await _open(db)
    row = await _claim(db)
    assert row is not None
    await _advance(db, deletion, row, "draining", cursor='{"page": 1}')
    await _app_rows(db, RETRY, {**_lease(deletion, row), "e": "boom", "delay": 60})
    again = await _claim(db)  # retry_at is in the future: the row is skipped until it passes
    assert again is None
    await _admin(db, "UPDATE tenant_deletions SET retry_at = now() - interval '1 second'")
    again = await _claim(db)
    assert again is not None
    await _advance(db, deletion, again, "draining", cursor='{"page": 2}', counts='{"n": 1}')
    record = await _record(db, deletion)
    assert (record.attempts, record.last_error, record.retry_at) == (0, None, None)
    assert (record.state, record.step_cursor, record.counts) == ("draining", {"page": 2}, {"n": 1})


async def test_attempts_are_bigint_everywhere(db: Db) -> None:
    """The column and the retry function's result are bigint."""
    column = (
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name = 'tenant_deletions' AND column_name = 'attempts'"
    )
    assert await _admin_scalar(db, column) == "bigint"
    result = (
        "SELECT pg_get_function_result("
        "'tb_retry_tenant_deletion(uuid, text, bigint, text, integer)'::regprocedure)"
    )
    assert await _admin_scalar(db, result) == "bigint"


async def test_the_lease_is_judged_by_the_wall_clock_not_the_transaction_start(db: Db) -> None:
    """A transaction that outlives the lease cannot use it (clock_timestamp, not now())."""
    await _drain(db)
    _, deletion = await _open(db)
    row = await _claim(db, ttl=1)
    assert row is not None
    with pytest.raises(DBAPIError, match=LOST):
        async with db.app() as session, session.begin():
            await session.execute(text("SELECT now()"))  # fixes the transaction's now()
            await session.execute(text("SELECT pg_sleep(1.5)"))
            await session.execute(text(RENEW), {**_lease(deletion, row), "ttl": 10})


# --- retry and release ---------------------------------------------------------------------


async def test_retry_records_the_error_counts_the_attempt_and_backs_off(db: Db) -> None:
    """Retry records the error counts the attempt and backs off."""
    await _drain(db)
    _, deletion = await _open(db)
    row = await _claim(db)
    assert row is not None
    args = {**_lease(deletion, row), "e": "boom " * 400, "delay": 3600}
    assert (await _app_rows(db, RETRY, args))[0][0] == 1
    record = await _record(db, deletion)
    assert (record.attempts, record.holder, record.expires_at) == (1, None, None)
    assert len(record.last_error) == 1000
    assert await _claim(db) is None  # backed off
    await _admin(db, "UPDATE tenant_deletions SET retry_at = now() - interval '1 second'")
    again = await _claim(db)
    assert again is not None
    assert (again.fence, again.attempts) == (2, 1)
    assert (await _app_rows(db, RETRY, {**_lease(deletion, again), "e": "x", "delay": 0}))[0][
        0
    ] == 2
    reclaimed = await _claim(db)  # delay 0: immediately claimable
    assert reclaimed is not None
    await _advance(db, deletion, reclaimed, "draining")
    record = await _record(db, deletion)
    assert (record.attempts, record.last_error, record.retry_at) == (0, None, None)
    for bad in ({"e": "", "delay": 1}, {"e": "x", "delay": -1}, {"e": "x", "delay": 86401}):
        with pytest.raises(DBAPIError, match="invalid retry arguments"):
            await _app_rows(db, RETRY, {**_lease(deletion, reclaimed), **bad})


async def test_release_frees_the_lease_for_the_next_claim(db: Db) -> None:
    """Release frees the lease for the next claim."""
    await _drain(db)
    _, deletion = await _open(db)
    row = await _claim(db)
    assert row is not None
    await _app_rows(db, RELEASE, _lease(deletion, row))
    again = await _claim(db, "w2")
    assert again is not None
    assert (again.holder, again.fence) == ("w2", 2)


# --- fail and cancel -----------------------------------------------------------------------


async def test_fail_is_terminal_and_a_new_request_starts_over(db: Db) -> None:
    """Fail is terminal and a new request starts over."""
    await _drain(db)
    tenant, deletion = await _open(db)
    row = await _claim(db)
    assert row is not None
    await _app_rows(db, FAIL, {**_lease(deletion, row), "r": "non-retryable"})
    record = await _record(db, deletion)
    assert (record.state, record.last_error, record.holder) == ("failed", "non-retryable", None)
    with pytest.raises(DBAPIError, match="deletion is terminal"):
        await _app_rows(db, FAIL, {**_lease(deletion, row), "r": "again"})
    # a failed deletion leaves the tenant disabled and a new request creates a new record
    again = await _request(db, tenant)
    assert again.outcome == "created"
    assert again.deletion_id != deletion
    assert await _admin_scalar(
        db, "SELECT admin_disabled_at IS NOT NULL FROM tenants WHERE id = :t", {"t": tenant.id}
    )
    # there is no lease-free path: a NULL holder or fence is an invalid argument
    for bad in ({"h": "w", "f": None}, {"h": None, "f": 1}, {"h": None, "f": None}):
        with pytest.raises(DBAPIError, match="invalid lease arguments"):
            await _app_rows(db, FAIL, {"d": again.deletion_id, "r": "x", **bad})
    with pytest.raises(DBAPIError, match="a reason is required"):
        await _app_rows(db, FAIL, {**_lease(again.deletion_id, row), "r": ""})


async def test_operator_fail_needs_no_lease_but_refuses_a_live_one(db: Db) -> None:
    """The operator function refuses a live lease, a terminal or missing record and bad input."""
    await _drain(db)
    _, deletion = await _open(db)
    row = await _claim(db)
    assert row is not None
    args = {"d": deletion, "a": "admin-7", "r": "stuck"}
    with pytest.raises(DBAPIError, match="deletion lease is held"):
        await _app_rows(db, OPERATOR_FAIL, args)
    assert (await _record(db, deletion)).state == "requested"
    await _admin(db, "UPDATE tenant_deletions SET expires_at = now() - interval '1 second'")
    await _app_rows(db, OPERATOR_FAIL, args)
    record = await _record(db, deletion)
    assert (record.state, record.holder, record.last_error) == (
        "failed",
        None,
        "operator admin-7: stuck",
    )
    with pytest.raises(DBAPIError, match="deletion is terminal"):  # the former holder
        await _app_rows(db, RENEW, {**_lease(deletion, row), "ttl": 10})
    with pytest.raises(DBAPIError, match="deletion is terminal"):
        await _app_rows(db, OPERATOR_FAIL, args)
    with pytest.raises(DBAPIError, match="deletion not found"):
        await _app_rows(db, OPERATOR_FAIL, {**args, "d": UUID(int=0)})
    for bad in ({"a": ""}, {"a": None}, {"r": ""}, {"r": None}):
        with pytest.raises(DBAPIError, match="an actor and a reason are required"):
            await _app_rows(db, OPERATOR_FAIL, {**args, **bad})


async def test_operator_fail_fails_a_released_or_unclaimed_deletion(db: Db) -> None:
    """A deletion nobody holds can be failed by the operator at once."""
    await _drain(db)
    _, deletion = await _open(db)
    await _app_rows(db, OPERATOR_FAIL, {"d": deletion, "a": "admin-7", "r": "x" * 2000})
    record = await _record(db, deletion)
    assert (record.state, len(record.last_error)) == ("failed", 1000)


async def test_fail_is_refused_once_the_tenant_row_is_gone(db: Db) -> None:
    """Fail is refused once the tenant row is gone."""
    tenant, deletion, row = await _in_database_step(db)
    await _app_rows(db, DELETE, {"t": tenant.id, "h": row.holder, "f": row.fence})
    with pytest.raises(DBAPIError, match="can only finish"):
        await _app_rows(db, FAIL, {**_lease(deletion, row), "r": "x"})
    await _admin(db, "UPDATE tenant_deletions SET expires_at = now() - interval '1 second'")
    with pytest.raises(DBAPIError, match="can only finish"):
        await _app_rows(db, OPERATOR_FAIL, {"d": deletion, "a": "admin-7", "r": "x"})
    assert (await _record(db, deletion)).state == "database"


async def test_cancel_works_only_from_requested_and_is_idempotent(db: Db) -> None:
    """Cancel works only from requested and is idempotent."""
    await _drain(db)
    tenant, deletion = await _open(db, not_before="1 hour")
    assert (await _app_rows(db, CANCEL, {"d": deletion}))[0][0] == "cancelled"
    assert (await _app_rows(db, CANCEL, {"d": deletion}))[0][0] == "cancelled"
    record = await _record(db, deletion)
    assert (record.state, record.holder) == ("cancelled", None)
    assert await _claim(db) is None
    assert await _admin_scalar(  # the tenant stays disabled
        db, "SELECT admin_disabled_at IS NOT NULL FROM tenants WHERE id = :t", {"t": tenant.id}
    )
    assert (await _request(db, tenant)).outcome == "created"  # a new record
    assert (await _app_rows(db, CANCEL, {"d": UUID(int=0)}))[0][0] == "not_found"
    await _drain(db)
    _, started = await _open(db)
    row = await _claim(db)
    assert row is not None
    await _advance(db, started, row, "draining")
    assert (await _app_rows(db, CANCEL, {"d": started}))[0][0] == "not_cancellable"
    await _admin(db, "UPDATE tenant_deletions SET state = 'done' WHERE id = :d", {"d": started})
    assert (await _app_rows(db, CANCEL, {"d": started}))[0][0] == "not_cancellable"


async def test_a_cancel_and_the_first_step_never_both_win(db: Db) -> None:
    """A cancel and the first step never both win."""
    await _drain(db)
    for _ in range(5):
        _, deletion = await _open(db)
        row = await _claim(db)
        assert row is not None
        results = await asyncio.gather(
            _app_rows(db, CANCEL, {"d": deletion}),
            _advance(db, deletion, row, "draining"),
            return_exceptions=True,
        )
        state = (await _record(db, deletion)).state
        if state == "cancelled":
            assert isinstance(results[1], DBAPIError)  # the engine learns the lease is void
        else:
            assert state == "draining"
            cancel = results[0]
            assert not isinstance(cancel, BaseException)
            assert cancel[0][0] == "not_cancellable"
        await _drain(db)


# --- migration -----------------------------------------------------------------------------


async def test_downgrade_refuses_while_a_non_terminal_deletion_exists() -> None:
    """Downgrade refuses while a non-terminal deletion exists and works once it is cancelled."""
    async for scratch in _provision(enforce=False):
        deletion = (await _request(scratch, await _tenant(scratch))).deletion_id
        refused = await _alembic(scratch.name, "downgrade", BEFORE)
        assert refused.returncode != 0
        assert "non-terminal tenant deletions exist" in refused.stderr
        assert await _admin_scalar(
            scratch, "SELECT to_regprocedure('tb_claim_tenant_deletion(text, integer)') IS NOT NULL"
        )
        await _admin(
            scratch,
            "UPDATE tenant_deletions SET state = 'cancelled' WHERE id = :d",
            {"d": deletion},
        )
        stepped = await _alembic(scratch.name, "downgrade", BEFORE)
        assert stepped.returncode == 0, stepped.stderr[-2000:]


async def test_downgrade_restores_the_previous_delete_function_and_drops_the_rest() -> None:
    """Downgrade restores the previous delete function and drops the rest."""
    async for scratch in _provision(enforce=False):
        stepped = await _alembic(scratch.name, "downgrade", BEFORE)
        assert stepped.returncode == 0, stepped.stderr[-2000:]
        one_arg = "SELECT to_regprocedure('tb_delete_tenant(uuid)') IS NOT NULL"
        assert await _admin_scalar(scratch, one_arg)
        assert await _admin_scalar(
            scratch, "SELECT to_regprocedure('tb_claim_tenant_deletion(text, integer)') IS NULL"
        )
        assert await _admin_scalar(
            scratch,
            "SELECT NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE "
            "table_name = 'tenant_deletions' AND column_name IN ('attempts', 'retry_at'))",
        )
        assert await _admin_scalar(
            scratch,
            "SELECT has_function_privilege('trustbridge_app', 'tb_delete_tenant(uuid)', 'EXECUTE')",
        )
        restored = await _alembic(scratch.name, "upgrade", REVISION)
        assert restored.returncode == 0, restored.stderr[-2000:]
        assert await _admin_scalar(
            scratch, "SELECT to_regprocedure('tb_delete_tenant(uuid)') IS NULL"
        )
        down = await _alembic(scratch.name, "downgrade", PREVIOUS)
        assert down.returncode == 0, down.stderr[-2000:]


async def test_the_hardening_downgrade_refuses_while_a_deletion_is_open_then_restores() -> None:
    """The downgrade refuses with an open deletion, then restores the bodies and the paths."""
    async for scratch in _provision(enforce=False):
        deletion = (await _request(scratch, await _tenant(scratch))).deletion_id
        refused = await _alembic(scratch.name, "downgrade", REVISION)
        assert refused.returncode != 0
        assert "non-terminal tenant deletions exist" in refused.stderr
        body = "SELECT pg_get_functiondef(CAST(:f AS regprocedure)) LIKE :m"
        advance = "tb_advance_tenant_deletion(uuid, text, bigint, text, jsonb, jsonb, jsonb)"
        assert await _admin_scalar(scratch, body, {"f": advance, "m": "%v_progress%"})
        await _admin(
            scratch,
            "UPDATE tenant_deletions SET state = 'cancelled' WHERE id = :d",
            {"d": deletion},
        )
        stepped = await _alembic(scratch.name, "downgrade", REVISION)
        assert stepped.returncode == 0, stepped.stderr[-2000:]
        assert not await _admin_scalar(scratch, body, {"f": advance, "m": "%v_progress%"})
        delete = "tb_delete_tenant(uuid, text, bigint)"
        assert not await _admin_scalar(scratch, body, {"f": delete, "m": "%webhook_deliveries%"})
        paths = (
            "SELECT array_agg(DISTINCT p.proconfig[1]) FROM pg_proc p WHERE p.prosecdef "
            "AND p.pronamespace = 'public'::regnamespace AND p.proname = ANY(:n)"
        )
        older = [
            "tb_resolve_tenant",
            "tb_upsert_tenant",
            "tb_list_tenants",
            "tb_get_tenant",
            "tb_set_tenant_disabled",
            "tb_request_tenant_deletion",
            "tb_audit_purge",
            "tb_audit_list",
            "tb_audit_get",
        ]
        count = "SELECT count(*) FROM pg_proc WHERE proname = ANY(:n) AND prosecdef"
        assert await _admin_scalar(scratch, count, {"n": older}) == len(older)
        assert await _admin_scalar(scratch, paths, {"n": older}) == [
            "search_path=pg_catalog, public"
        ]
        restored = await _alembic(scratch.name, "upgrade", HARDENING)
        assert restored.returncode == 0, restored.stderr[-2000:]
        assert await _admin_scalar(scratch, body, {"f": advance, "m": "%v_progress%"})
        assert await _admin_scalar(scratch, body, {"f": delete, "m": "%webhook_deliveries%"})
        assert await _admin_scalar(scratch, paths, {"n": older}) == [
            "search_path=pg_catalog, public, pg_temp"
        ]


async def _drift(db: Db, signature: str) -> None:
    """Edit the live function the way a hotfix would: one comment inside its body."""
    definition = await _admin_scalar(
        db, "SELECT pg_get_functiondef(CAST(:f AS regprocedure))", {"f": signature}
    )
    assert "\nBEGIN\n" in definition
    async with db.admin.begin() as conn:
        await conn.exec_driver_sql(definition.replace("\nBEGIN\n", "\nBEGIN\n    -- drifted\n", 1))


@pytest.mark.parametrize(
    "signature",
    [
        "tb_advance_tenant_deletion(uuid, text, bigint, text, jsonb, jsonb, jsonb)",
        "tb_delete_tenant(uuid, text, bigint)",
    ],
)
async def test_the_hardening_upgrade_refuses_a_drifted_body_and_changes_nothing(
    signature: str,
) -> None:
    """A body that differs anywhere from the 5d7a2c8e1b94 text is refused, not overwritten."""
    async for scratch in _provision(enforce=False):
        stepped = await _alembic(scratch.name, "downgrade", REVISION)
        assert stepped.returncode == 0, stepped.stderr[-2000:]
        await _drift(scratch, signature)
        refused = await _alembic(scratch.name, "upgrade", HARDENING)
        assert refused.returncode != 0
        assert f"{signature} is not the 5d7a2c8e1b94 body" in refused.stderr
        assert await _admin_scalar(
            scratch,
            "SELECT prosrc LIKE '%-- drifted%' FROM pg_proc WHERE oid = CAST(:f AS regprocedure)",
            {"f": signature},
        )
        paths = (
            "SELECT count(*) FROM pg_proc WHERE prosecdef "
            "AND pronamespace = 'public'::regnamespace "
            "AND NOT proconfig @> ARRAY['search_path=pg_catalog, public, pg_temp']"
        )
        assert await _admin_scalar(scratch, paths) > 0  # the transaction rolled back


@pytest.mark.parametrize(
    "signature",
    [
        "tb_advance_tenant_deletion(uuid, text, bigint, text, jsonb, jsonb, jsonb)",
        "tb_delete_tenant(uuid, text, bigint)",
    ],
)
async def test_the_hardening_downgrade_refuses_a_drifted_body_and_changes_nothing(
    signature: str,
) -> None:
    """The downgrade restores only this revision's exact bodies."""
    async for scratch in _provision(enforce=False):
        await _drift(scratch, signature)
        refused = await _alembic(scratch.name, "downgrade", REVISION)
        assert refused.returncode != 0
        assert f"{signature} is not the 6f3b9d2e4a85 body" in refused.stderr
        assert await _admin_scalar(
            scratch,
            "SELECT prosrc LIKE '%-- drifted%' FROM pg_proc WHERE oid = CAST(:f AS regprocedure)",
            {"f": signature},
        )
        still = "SELECT count(*) FROM pg_proc WHERE proname = 'tb_get_tenant' AND proconfig @> :p"
        assert (
            await _admin_scalar(scratch, still, {"p": ["search_path=pg_catalog, public, pg_temp"]})
            == 1
        )
