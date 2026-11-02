"""The ports over NG3's components: tenants, parked values, connection data, reconcile requests."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import SecretStr

from tests.trustbridge.fakes import FakeRedis
from tests.trustbridge.helpers import as_redis, host
from trustbridge.adapters.interface import IssuedSecret
from trustbridge.worker.ports import (
    PRIVILEGED_KEY,
    ConnectionData,
    MemoryTenants,
    ParkedValues,
    ReconcileRequests,
    UnknownTenantError,
)
from trustbridge.worker.secrets import MemorySecrets


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
