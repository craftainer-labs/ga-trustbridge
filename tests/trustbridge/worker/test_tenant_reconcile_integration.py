"""One full tenant reconcile against Postgres and Valkey (NG5r-b).

The credential store is the SQL one over the stack's Postgres, the request queue is the real one
over Valkey (the Lua scripts run on the server), and the platform is the fake adapter. The audit
observer is not composed: the audit tables are not part of this fixture, and the observer is
covered with the store in the audit tests.
"""

import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from typing import cast
from uuid import UUID

import pytest
from craftainer_crud.models.base import Base
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.config import Settings
from app.db import build_engine
from tests.trustbridge.adapters.conftest import PULL, PUSH_PROD, build_world
from tests.trustbridge.worker.harness import Harness
from tests.trustbridge.worker.test_reconcile_queue_integration import keys
from trustbridge.adapters.core import CredentialCore, RetryableOperationError
from trustbridge.adapters.fake import fake_capabilities
from trustbridge.adapters.models import (
    AdapterCredentialRow,
    AdapterResourceRow,
    CredentialOperationRow,
)
from trustbridge.adapters.sql_store import SqlCredentialStore
from trustbridge.adapters.store import OutputError
from trustbridge.worker.reconcile_queue import ReconcileQueue
from trustbridge.worker.reconciler import Reconciler, ReconcilerDeps
from trustbridge.worker.tenant_reconcile import CredentialReconciler

pytestmark = pytest.mark.integration

TABLES: list[Table] = [
    cast("Table", m.__table__)
    for m in (AdapterResourceRow, AdapterCredentialRow, CredentialOperationRow)
]


class Allowed:
    """`TenantRefusals` that refuses nobody."""

    async def refusal_of(self, tenant_id: UUID) -> str | None:
        """The tenant may be reconciled."""
        del tenant_id
        return None


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """An engine with the credential tables present and empty."""
    eng = build_engine(Settings(mode="dev"))
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=TABLES, checkfirst=True)
        for table in TABLES:
            await conn.execute(table.delete())
    yield eng
    async with eng.begin() as conn:
        for table in TABLES:
            await conn.execute(table.delete())
    await eng.dispose()


async def test_a_tenant_with_stored_credentials_is_reconciled_in_one_pass(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """Resume, eligibility, both sweeps and the ack happen in one pass; a repeat is a no-op."""
    world = build_world(fake_capabilities(oidc=True))
    store = SqlCredentialStore(async_sessionmaker(engine, expire_on_commit=False))
    core = CredentialCore(replace(world.deps, store=store))
    tenant = world.tenant
    world.inspector.protected.add((10, "prod"))
    await core.ensure_scope(tenant, "fake", PUSH_PROD)  # a trust credential, eligible for now
    world.writer.fail_next = [OutputError("down")]
    with pytest.raises(RetryableOperationError):
        await core.ensure_scope(tenant, "fake", PULL)  # leaves an open operation
    assert len(await store.list_open_operations()) == 1
    world.inspector.protected.clear()  # the environment lost its protection
    orphan = f"tb-{tenant.id.hex}-deadbeefdeadbeef-g1"
    world.platform.credentials[orphan] = {"id": "x", "kind": "secret", "secret": "s"}

    h = await Harness({}, tenant_reconcile_enabled=True).start()
    async with keys() as (client, names):
        queue = ReconcileQueue(client)
        recon = Reconciler(
            h.settings,
            ReconcilerDeps(
                store=h.store,
                queue=h.queue,
                relay=h.relay,
                hosts=h.hosts,
                leases=h.leases,
                deliveries=None,
                clock=h.clock,
                reconcile_requests=queue,
                refusals=Allowed(),
                tenant_reconciler=CredentialReconciler(core, store, world.deps.tenants, ["fake"]),
            ),
        )
        await client.sadd(names["RECONCILE_SET"], str(tenant.id))
        with caplog.at_level(logging.WARNING):
            assert await recon.tenant_reconcile() == 1
        assert "failed" not in caplog.text
        assert (await queue.pending(), await queue.inflight()) == (0, 0)
        for name in ("CLAIMS_HASH", "ATTEMPTS_HASH"):
            assert await client.hlen(names[name]) == 0

        assert await store.list_open_operations() == []
        rows = {c.scope.permission: c for c in await store.list_credentials(tenant.id, "fake")}
        assert rows["push"].kind == "secret"  # eligibility re-evaluated: the trust was switched
        assert set(rows) == {"push", "pull"}  # and no scope was created
        assert orphan not in world.platform.credentials
        assert world.values.parked == {}

        # a second request finds nothing to change
        before = (dict(world.platform.credentials), rows)
        h.advance(timedelta(minutes=1).total_seconds())
        await client.sadd(names["RECONCILE_SET"], str(tenant.id))
        assert await recon.tenant_reconcile() == 1
        again = {c.scope.permission: c for c in await store.list_credentials(tenant.id, "fake")}
        assert (dict(world.platform.credentials), again) == before
