"""The ports over NG3's components: tenants, parked values, connection data, reconcile requests."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy.exc import DBAPIError

from tests.trustbridge.fakes import FakeRedis
from tests.trustbridge.helpers import as_redis, host
from trustbridge.adapters.interface import IssuedSecret, Tenant
from trustbridge.github.ports import TenantDeletedError
from trustbridge.worker.ports import (
    PRIVILEGED_KEY,
    ConnectionData,
    MemoryTenants,
    ParkedValues,
    ReconcileRequests,
    SqlTenants,
    UnknownTenantError,
)
from trustbridge.worker.secrets import MemorySecrets, SecretUnavailable


def _tenants() -> MemoryTenants:
    projection = host().to_host()
    return MemoryTenants(lambda _name: projection)


async def test_installation_lifecycle_in_memory() -> None:
    """Installation lifecycle in memory."""
    tenants = _tenants()
    tenant_id = await tenants.upsert_installation("github.com", 7, "acme", 1)
    assert await tenants.upsert_installation("github.com", 7, "acme2", 1) == tenant_id
    assert await tenants.tenant_id_for("github.com", 7) == tenant_id
    assert await tenants.tenant_id_for("github.com", 8) is None
    assert await tenants.installation_id(tenant_id) == 7
    tenant = await tenants.get_tenant(tenant_id)
    assert (tenant.id, tenant.org, tenant.github_host.name) == (tenant_id, "acme2", "github.com")
    assert await tenants.set_installation_state("github.com", 7, "suspended") == tenant_id
    assert await tenants.set_installation_state("github.com", 8, "suspended") is None


async def test_a_deleted_installation_is_refused_by_an_upsert_in_memory() -> None:
    """A deleted installation keeps its id, state and org; the upsert raises instead."""
    tenants = _tenants()
    tenant_id = await tenants.upsert_installation("github.com", 7, "acme", 1)
    await tenants.set_installation_state("github.com", 7, "deleted")
    with pytest.raises(TenantDeletedError) as refused:
        await tenants.upsert_installation("github.com", 7, "acme2", 2)
    assert refused.value.reason == "deleted"
    tenant = await tenants.get_tenant(tenant_id)
    assert tenant.org == "acme"
    assert tenants._rows[tenant_id]["state"] == "deleted"


async def test_a_tombstoned_installation_is_refused_and_others_are_not_in_memory() -> None:
    """A `done` deletion of the key refuses the upsert; another installation id is unaffected."""
    tenants = _tenants()
    tenants.tombstones.add(("github.com", 7))
    with pytest.raises(TenantDeletedError) as refused:
        await tenants.upsert_installation("github.com", 7, "acme", 1)
    assert (refused.value.host, refused.value.installation_id) == ("github.com", 7)
    assert refused.value.reason == "tombstone"
    assert await tenants.tenant_id_for("github.com", 7) is None
    assert await tenants.upsert_installation("github.com", 8, "acme", 1) is not None
    assert await tenants.upsert_installation("other.host", 7, "acme", 1) is not None


async def test_an_installation_being_deleted_is_refused_and_others_are_not_in_memory() -> None:
    """A non-terminal deletion refuses the upsert and changes nothing; other keys are unaffected."""
    tenants = _tenants()
    tenant_id = await tenants.upsert_installation("github.com", 7, "acme", 1)
    tenants.deleting.add(("github.com", 7))
    with pytest.raises(TenantDeletedError) as refused:
        await tenants.upsert_installation("github.com", 7, "acme2", 2, suspended=True)
    assert refused.value.reason == "deleting"
    assert (await tenants.get_tenant(tenant_id)).org == "acme"
    assert tenants._rows[tenant_id]["state"] == "active"
    assert await tenants.upsert_installation("github.com", 8, "acme", 1) is not None


async def test_the_state_setter_and_the_active_lookup_refuse_the_same_keys_in_memory() -> None:
    """Mirrors `SqlTenants`: tombstoned, deleting and `deleted` keys raise, others do not."""
    tenants = _tenants()
    tenant_id = await tenants.upsert_installation("github.com", 7, "acme", 1)
    assert await tenants.active_tenant_id_for("github.com", 7) == tenant_id
    assert await tenants.active_tenant_id_for("github.com", 8) is None
    tenants.deleting.add(("github.com", 7))
    for call in (
        tenants.active_tenant_id_for("github.com", 7),
        tenants.set_installation_state("github.com", 7, "suspended"),
    ):
        with pytest.raises(TenantDeletedError) as deleting:
            await call
        assert deleting.value.reason == "deleting"
    tenants.deleting.clear()
    tenants.tombstones.add(("github.com", 7))
    with pytest.raises(TenantDeletedError) as tombstone:
        await tenants.set_installation_state("github.com", 7, "deleted")
    assert tombstone.value.reason == "tombstone"
    assert tenants._rows[tenant_id]["state"] == "active"
    tenants.tombstones.clear()
    assert await tenants.set_installation_state("github.com", 7, "deleted") == tenant_id
    # `deleted` on `deleted` is a no-op that still answers; any other state is refused
    assert await tenants.set_installation_state("github.com", 7, "deleted") == tenant_id
    with pytest.raises(TenantDeletedError) as gone:
        await tenants.set_installation_state("github.com", 7, "active")
    assert gone.value.reason == "deleted"
    with pytest.raises(TenantDeletedError):
        await tenants.active_tenant_id_for("github.com", 7)
    assert tenants._rows[tenant_id]["state"] == "deleted"


async def test_repository_changes_are_applied_renamed_and_removed_in_memory() -> None:
    """Repository changes are applied renamed and removed in memory."""
    tenants = _tenants()
    tenant_id = await tenants.upsert_installation("github.com", 7, "acme", 1)
    await tenants.apply_repository_changes(tenant_id, [(1, "acme/a"), (2, "acme/b")], [])
    await tenants.apply_repository_changes(tenant_id, [(3, "acme/c")], [1])
    await tenants.rename_repository(tenant_id, 2, "acme/b2")
    await tenants.rename_repository(tenant_id, 99, "acme/unknown")  # an unknown id is ignored
    assert tenants.repositories[tenant_id] == {2: "acme/b2", 3: "acme/c"}


async def test_an_unknown_tenant_is_an_error() -> None:
    """An unknown tenant is an error."""
    tenants = _tenants()
    with pytest.raises(UnknownTenantError):
        await tenants.installation_id(uuid4())
    with pytest.raises(UnknownTenantError):
        await tenants.get_tenant(uuid4())


async def test_parked_values_round_trip_through_the_secret_store_and_list() -> None:
    """Parked values round trip through the secret store and list."""
    secrets = MemorySecrets()
    parked = ParkedValues(secrets)
    first, second = uuid4(), uuid4()
    expires = datetime(2027, 1, 1, tzinfo=UTC)
    await parked.put(first, IssuedSecret({"token": SecretStr("t0p")}, expires))
    await parked.put(second, IssuedSecret({"a": SecretStr("1")}))
    got = await parked.get(first)
    assert got is not None
    assert (got.values["token"].get_secret_value(), got.expires_at) == ("t0p", expires)
    other = await parked.get(second)
    assert other is not None
    assert other.expires_at is None
    assert sorted(await parked.list_ids()) == sorted([first, second])
    await parked.delete(first)
    await parked.delete(first)  # absent is fine
    assert await parked.get(first) is None
    assert list(await parked.list_ids()) == [second]


async def test_connection_data_merges_and_splits_out_the_privileged_credential() -> None:
    """Connection data merges and splits out the privileged credential."""
    connections = ConnectionData(MemorySecrets())
    tenants = _tenants()
    tenant_id = await tenants.upsert_installation("github.com", 7, "acme", 1)
    tenant = await tenants.get_tenant(tenant_id)
    empty = await connections.config_for(tenant, "jfrog")
    assert (dict(empty.connection), empty.privileged_credential) == ({}, None)
    await connections.put(tenant_id, "jfrog", {"url": SecretStr("https://j.example")})
    await connections.put(
        tenant_id, "jfrog", {PRIVILEGED_KEY: SecretStr("admin"), "x": SecretStr("1")}
    )
    config = await connections.config_for(tenant, "jfrog")
    assert sorted(config.connection) == ["url", "x"]
    assert config.privileged_credential is not None
    assert config.privileged_credential.get_secret_value() == "admin"
    assert "admin" not in repr(config)


async def test_reconcile_requests_go_to_valkey_or_stay_local() -> None:
    """Reconcile requests go to valkey or stay local."""
    valkey = FakeRedis()
    tenant_id = uuid4()
    await ReconcileRequests(as_redis(valkey)).schedule(tenant_id)
    await ReconcileRequests(as_redis(valkey)).schedule(tenant_id)
    assert valkey.sets == {"tb:reconcile": {str(tenant_id)}}
    local = ReconcileRequests()
    await local.schedule(tenant_id)
    assert local.local == {str(tenant_id)}


async def test_connection_data_put_during_an_outage_fails_and_never_overwrites() -> None:
    """An unavailable store is not "absent": `put` raises and the stored blob survives."""
    secrets = MemorySecrets()
    connections = ConnectionData(secrets)
    tenant_id = uuid4()
    await connections.put(tenant_id, "jfrog", {"url": SecretStr("https://j.example")})
    before = dict(secrets.values)
    secrets.down = True
    with pytest.raises(SecretUnavailable):
        await connections.put(tenant_id, "jfrog", {"x": SecretStr("1")})
    secrets.down = False
    assert secrets.values == before


async def test_connection_data_concurrent_puts_do_not_lose_updates() -> None:
    """Concurrent puts to one key are serialised; the lock is dropped when idle."""

    class SlowSecrets(MemorySecrets):
        async def read(self, ref: str) -> SecretStr:
            await asyncio.sleep(0.01)
            return await super().read(ref)

    connections = ConnectionData(SlowSecrets())
    tenant_id = uuid4()
    await asyncio.gather(
        *(connections.put(tenant_id, "jfrog", {f"k{i}": SecretStr(str(i))}) for i in range(5))
    )
    tenant = Tenant(id=tenant_id, github_host=host().to_host(), org="acme")
    config = await connections.config_for(tenant, "jfrog")
    assert sorted(config.connection) == [f"k{i}" for i in range(5)]
    assert len(connections._locks) == 0


class _OrigError(Exception):
    def __init__(self, sqlstate: str | None) -> None:
        super().__init__("db error")
        self.sqlstate = sqlstate


@pytest.mark.parametrize(
    ("sqlstate", "reason"),
    [("TB001", "tombstone"), ("TB002", "deleting"), ("22023", None), (None, None)],
)
def test_the_upsert_refusals_map_to_tenant_deleted_errors(
    sqlstate: str | None, reason: str | None
) -> None:
    """`tb_upsert_tenant` raises TB001 (tombstone) and TB002 (deleting); others are not ours."""
    error = DBAPIError("SELECT tb_upsert_tenant(...)", None, _OrigError(sqlstate))
    mapped = SqlTenants.deleted_error("github.com", 7, error)
    if reason is None:
        assert mapped is None
    else:
        assert mapped is not None
        assert (mapped.host, mapped.installation_id, mapped.reason) == ("github.com", 7, reason)
