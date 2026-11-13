"""Every `SECURITY DEFINER` function pins `pg_temp` last in its `search_path` (ADR 0031).

The PostgreSQL documentation ("Writing SECURITY DEFINER Functions Safely") says a `pg_temp` that is
not listed is searched first for relations, so a definer must list it last. Migration
`6f3b9d2e4a85` put it on the older definers; this test fails CI for any definer added later
without it. Runs in CI's stack (integration), never in the unit lane, over the real Alembic chain.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

from tests.trustbridge.tenancy.test_rls_integration import Db, _provision

pytestmark = pytest.mark.integration

PIN = "search_path=pg_catalog, public, pg_temp"
EXPECTED = {
    "tb_resolve_tenant",
    "tb_upsert_tenant",
    "tb_list_tenants",
    "tb_get_tenant",
    "tb_set_tenant_disabled",
    "tb_request_tenant_deletion",
    "tb_audit_purge",
    "tb_audit_list",
    "tb_audit_get",
    "tb_claim_tenant_deletion",
    "tb_renew_tenant_deletion",
    "tb_advance_tenant_deletion",
    "tb_retry_tenant_deletion",
    "tb_release_tenant_deletion",
    "tb_fail_tenant_deletion",
    "tb_operator_fail_tenant_deletion",
    "tb_cancel_tenant_deletion",
    "tb_finish_tenant_deletion",
    "tb_delete_tenant",
    "tb_purge_tenant_deletions",
}


@pytest.fixture(scope="module")
async def db() -> AsyncIterator[Db]:
    """The shipped schema, as the application's migrations leave it."""
    async for provisioned in _provision(enforce=False):
        yield provisioned


async def test_every_definer_function_has_pg_temp_last_in_its_search_path(db: Db) -> None:
    """No definer in the schema (the owner role's too) may leave `pg_temp` unlisted or not last."""
    async with db.admin.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT p.oid::regprocedure::text AS signature, p.proname, p.proconfig "
                    "FROM pg_proc p WHERE p.prosecdef "
                    "AND p.pronamespace = 'public'::regnamespace ORDER BY 1"
                )
            )
        ).all()
    assert {row.proname for row in rows} >= EXPECTED  # the query sees the functions at all
    wrong = [
        row.signature
        for row in rows
        if [c for c in row.proconfig or () if c.startswith("search_path=")] != [PIN]
    ]
    assert wrong == [], f"SECURITY DEFINER functions without pg_temp last in search_path: {wrong}"
