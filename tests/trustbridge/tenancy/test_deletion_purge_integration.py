"""The retention purge of finished deletion records against a real Postgres (NG6h-3c-2, ADR 0034).

`tb_purge_tenant_deletions` (migration `7b2d4f8a1c36`) and `SqlRecordPurger`: `done` records past
the cut-off whose tombstone exists, and old `cancelled` records, are deleted; the tombstone survives,
nothing else is touched, the arguments are validated, and the one-way door (the `4e9a1c7b5d23`
downgrade refuses once records are purged) holds. Runs in CI's stack (integration), never in the unit lane.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.trustbridge.tenancy.test_deletion_db_integration import (
    BEFORE_TOMBSTONES,
    TOMBSTONES,
    Tenant,
    _admin,
    _admin_rows,
    _admin_scalar,
    _alembic,
    _app_rows,
    _no_hosts,
    _request,
    _tenant,
    _upsert_refusal,
)
from tests.trustbridge.tenancy.test_rls_integration import OWNER_ROLE, Db, _provision
from trustbridge.deletion.purge import SqlRecordPurger
from trustbridge.github.ports import TenantDeletedError
from trustbridge.worker.ports import SqlTenants

pytestmark = pytest.mark.integration

PURGE_REVISION = "7b2d4f8a1c36"
SIGNATURE = "tb_purge_tenant_deletions(timestamptz, integer, integer)"
PURGE = "SELECT purged, unprotected FROM tb_purge_tenant_deletions(:before, :floor, :batch)"
NOW = datetime.now(UTC)


@pytest.fixture(scope="module")
async def db() -> AsyncIterator[Db]:
    """The shipped schema with RLS forced; the purge function runs as the owner role."""
    async for provisioned in _provision(enforce=True):
        async with provisioned.admin.begin() as conn:
            await conn.execute(text(f"ALTER FUNCTION {SIGNATURE} OWNER TO {OWNER_ROLE}"))
        yield provisioned


async def _finished(
    db: Db, *, days: int, tombstone: bool = True, state: str = "done"
) -> tuple[Tenant, UUID]:
    """A tenant whose deletion record is `state`, updated `days` ago; its tenant row is gone."""
    tenant = await _tenant(db)
    deletion = (await _request(db, tenant)).deletion_id
    await _admin(db, "DELETE FROM tenants WHERE id = :t", {"t": tenant.id})
    await _admin(
        db,
        "UPDATE tenant_deletions SET state = :s, updated_at = now() - make_interval(days => :n) "
        "WHERE id = :d",
        {"s": state, "n": days, "d": deletion},
    )
    if tombstone:
        await _admin(
            db,
            "INSERT INTO tenant_tombstones (host, installation_id, tenant_id) VALUES (:h, :i, :t)",
            {"h": tenant.host, "i": tenant.installation, "t": tenant.id},
        )
    return tenant, deletion


async def _exists(db: Db, deletion: UUID) -> bool:
    return bool(
        await _admin_scalar(
            db, "SELECT count(*) FROM tenant_deletions WHERE id = :d", {"d": deletion}
        )
    )


async def _purge(db: Db, *, days: int = 100, floor: int = 90, batch: int = 1000) -> tuple[int, int]:
    row = (
        await _app_rows(
            db, PURGE, {"before": NOW - timedelta(days=days), "floor": floor, "batch": batch}
        )
    )[0]
    return row.purged, row.unprotected


async def test_only_old_done_records_with_a_tombstone_are_purged_and_the_tombstone_stays(
    db: Db,
) -> None:
    """Old `done` goes; young `done`, open and failed stay; the tombstone is kept."""
    old, old_id = await _finished(db, days=200)
    _, young_id = await _finished(db, days=10)
    _, failed_id = await _finished(db, days=200, state="failed")
    _, running_id = await _finished(db, days=200, state="database")
    purged, _ = await _purge(db)
    assert purged >= 1
    assert not await _exists(db, old_id)
    for kept in (young_id, failed_id, running_id):
        assert await _exists(db, kept)
    key = {"h": old.host, "i": old.installation}
    assert (
        await _admin_scalar(
            db,
            "SELECT count(*) FROM tenant_tombstones WHERE host = :h AND installation_id = :i",
            key,
        )
        == 1
    )
    # B2 still holds with the record gone: the function, the store and the key are refused
    upsert = "SELECT tb_upsert_tenant(:h, :i, 'o', 1, 'active')"
    assert await _upsert_refusal(db, upsert, key) == "tombstone"
    with pytest.raises(TenantDeletedError):
        await SqlTenants(db.app, _no_hosts).upsert_installation(old.host, old.installation, "o", 1)


async def test_cancelled_records_follow_the_retention_without_a_tombstone(db: Db) -> None:
    """An old `cancelled` record goes with no tombstone check; a young one stays."""
    old, old_id = await _finished(db, days=200, tombstone=False, state="cancelled")
    _, young_id = await _finished(db, days=10, tombstone=False, state="cancelled")
    purged, _ = await _purge(db)
    assert purged >= 1
    assert not await _exists(db, old_id)
    assert await _exists(db, young_id)
    # the tenant was never tombstoned, and the purge created and removed nothing of the kind
    assert not await _admin_scalar(
        db,
        "SELECT count(*) FROM tenant_tombstones WHERE host = :h AND installation_id = :i",
        {"h": old.host, "i": old.installation},
    )


async def test_a_done_record_without_a_tombstone_is_never_deleted_and_is_counted(db: Db) -> None:
    """The guard: the purge cannot remove the last tombstone record; it reports it instead."""
    _, bare_id = await _finished(db, days=200, tombstone=False)
    _, safe_id = await _finished(db, days=200)
    purged, unprotected = await _purge(db)
    assert purged >= 1
    assert unprotected >= 1
    assert await _exists(db, bare_id)
    assert not await _exists(db, safe_id)
    await _admin(db, "DELETE FROM tenant_deletions WHERE id = :d", {"d": bare_id})


async def test_a_batch_bounds_the_purge_oldest_first(db: Db) -> None:
    """At most `batch` records per call, the oldest first; the next call takes the rest."""
    await _purge(db)  # clear what earlier tests left
    ids = [(await _finished(db, days=300 - n))[1] for n in range(3)]  # ids[0] is the oldest
    assert (await _purge(db, batch=2))[0] == 2
    assert [await _exists(db, i) for i in ids] == [False, False, True]
    assert (await _purge(db, batch=2))[0] == 1
    assert (await _purge(db, batch=2))[0] == 0


@pytest.mark.parametrize(
    ("days", "floor", "batch", "message"),
    [
        (100, 0, 10, "invalid retention floor"),
        (100, 90, 0, "invalid batch size"),
        (100, 90, 10001, "invalid batch size"),
        (10, 90, 10, "inside the retention floor"),
    ],
)
async def test_the_arguments_are_validated(
    db: Db, days: int, floor: int, batch: int, message: str
) -> None:
    """A floor below 1, a batch outside 1..10000 and a cut-off inside the floor are refused."""
    with pytest.raises(DBAPIError, match=message):
        await _purge(db, days=days, floor=floor, batch=batch)
    with pytest.raises(DBAPIError, match="inside the retention floor"):
        await _app_rows(db, PURGE, {"before": None, "floor": 90, "batch": 10})


async def test_the_function_is_a_pinned_definer_for_the_app_role_only(db: Db) -> None:
    """EXECUTE is revoked from PUBLIC, the app role has it and the table is still read-only."""
    row = (
        await _admin_rows(
            db,
            "SELECT has_function_privilege('trustbridge_app', :f, 'EXECUTE') AS app, "
            "has_function_privilege('public', :f, 'EXECUTE') AS public, "
            "p.prosecdef AS definer, "
            "p.proconfig @> ARRAY['search_path=pg_catalog, public, pg_temp'] AS pinned "
            "FROM pg_proc p WHERE p.oid = CAST(:f AS regprocedure)",
            {"f": SIGNATURE},
        )
    )[0]
    assert (row.app, row.public, row.definer, row.pinned) == (True, False, True, True)
    with pytest.raises(DBAPIError, match="permission denied"):
        await _app_rows(db, "DELETE FROM tenant_deletions", {})
    assert await _admin_scalar(
        db,
        "SELECT has_table_privilege('trustbridge_owner', 'tenant_deletions', 'DELETE')",
    )


async def test_the_purger_deletes_through_the_function_and_reports_unprotected(db: Db) -> None:
    """`SqlRecordPurger` end to end: retention 0 keeps everything, a retention purges."""
    await _purge(db)
    _, kept = await _finished(db, days=200)
    forever = SqlRecordPurger(db.app, retention_days=0, min_days=90, batch_size=10, max_batches=2)
    assert (await forever.purge()).purged == 0
    assert await _exists(db, kept)
    purger = SqlRecordPurger(db.app, retention_days=100, min_days=90, batch_size=10, max_batches=2)
    result = await purger.purge()
    assert (result.purged, result.unprotected) == (1, 0)
    assert not await _exists(db, kept)


async def test_purging_is_a_one_way_door_for_the_tombstone_migration() -> None:
    """After a purge the `4e9a1c7b5d23` downgrade refuses; this revision's own downgrade works."""
    async for scratch in _provision(enforce=False):
        await _admin(scratch, f"ALTER FUNCTION {SIGNATURE} OWNER TO {OWNER_ROLE}")
        tenant, deletion = await _finished(scratch, days=200)
        assert (await _purge(scratch))[0] == 1
        refused = await _alembic(scratch.name, "downgrade", BEFORE_TOMBSTONES)
        assert refused.returncode != 0
        assert "tombstone has no done deletion record" in refused.stderr
        assert await _admin_scalar(scratch, "SELECT to_regclass('tenant_tombstones') IS NOT NULL")
        # this revision alone goes down cleanly (the function and the grant), and back up
        stepped = await _alembic(scratch.name, "downgrade", TOMBSTONES)
        assert stepped.returncode == 0, stepped.stderr[-2000:]
        assert await _admin_scalar(scratch, f"SELECT to_regprocedure('{SIGNATURE}') IS NULL")
        again = await _alembic(scratch.name, "upgrade", PURGE_REVISION)
        assert again.returncode == 0, again.stderr[-2000:]
        assert await _admin_scalar(scratch, f"SELECT to_regprocedure('{SIGNATURE}') IS NOT NULL")
        assert (
            await _admin_scalar(
                scratch,
                "SELECT count(*) FROM tenant_tombstones WHERE host = :h",
                {"h": tenant.host},
            )
            == 1
        )
        assert not await _exists(scratch, deletion)
