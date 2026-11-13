"""Implementations of the GitHub core's and the adapter core's ports over NG3's components.

- `SqlTenants` / `MemoryTenants`: `InstallationStore`, `InstallationResolver` and the adapter
  core's `TenantResolver` over the `tenants` and `tenant_repositories` tables (the memory twin
  serves `MODE=mock` and the unit tests).
- `ParkedValues`: the adapter core's `ValueStore` in the secret store (`store:parked/<id>`),
  never in Postgres (ADR 0029).
- `ConnectionData`: `TenantConnectionStore` and the adapter core's `ConfigResolver`; one JSON
  object per `(tenant, adapter)` at `store:tenants/<tenant>/<adapter>`; the reserved key
  `privileged_credential` becomes `AdapterConfig.privileged_credential`.
- `ReconcileRequests`: `ReconcileScheduler`; tenant ids are added to the Valkey set
  `tb:reconcile` (a process-local set without Valkey). Consuming the set is the reconcile
  loop's job (NG5r); until then requests are recorded, not acted on.
- The lease needs no adapter: `trustbridge.worker.lease.LeaseManager.hold` and `Lease.guard`
  satisfy the core's `LeaseProvider` and `Lease`.
"""

import asyncio
import json
import weakref
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import Row, case, delete, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.adapters.interface import AdapterConfig, GitHubHost, IssuedSecret, Tenant
from trustbridge.github.ports import TenantDeletedError
from trustbridge.worker.models import Tenant as TenantRow
from trustbridge.worker.models import TenantDeletion, TenantRepository, TenantTombstone
from trustbridge.worker.secrets import SecretNotFound, SecretUnavailable

PARKED_PREFIX = "parked/"
PRIVILEGED_KEY = "privileged_credential"
RECONCILE_SET = "tb:reconcile"
HostLookup = Callable[[str], GitHubHost]
# The SQLSTATEs `tb_upsert_tenant` raises (migration 3b8d6f2a4c17) and the reason each maps to.
_UPSERT_REFUSALS = {"TB001": "tombstone", "TB002": "deleting"}


class UnknownTenantError(LookupError):
    """No tenant has this id."""


class WritableSecrets(Protocol):
    """What the value store and the connection data need from the secret store."""

    async def read(self, ref: str) -> SecretStr:
        """Read a secret."""
        ...

    async def write(self, ref: str, value: SecretStr) -> None:
        """Write a secret."""
        ...

    async def delete(self, ref: str) -> None:
        """Delete a secret (absent is fine)."""
        ...

    async def list_refs(self, prefix: str) -> list[str]:
        """List the `store:` references under a name prefix."""
        ...


class SqlTenants:
    """Tenant bookkeeping in Postgres."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], hosts: HostLookup
    ) -> None:
        """`hosts` maps a host name to its adapter-facing projection."""
        self._sf = session_factory
        self._hosts = hosts

    async def upsert_installation(
        self, host: str, installation_id: int, org: str, org_id: int, *, suspended: bool = False
    ) -> UUID:
        """Create or update the tenant of `(host, installation_id)` as `suspended` or `active`.

        `TenantDeletedError` is raised, and nothing changes, for a tenant whose state is `deleted`
        (`reason` "deleted"), one with a non-terminal deletion ("deleting"), and a key with a
        `tenant_tombstones` row (or a `database`-step deletion whose tenant row is gone), a
        tombstone ("tombstone"): a late or replayed event cannot revive or recreate it (ADR 0034).
        The checks run under the same per-installation advisory lock as `tb_upsert_tenant` and
        `tb_delete_tenant` (`tb_tenant:<host>:<installation_id>`, hashed with
        `hashtextextended`), so a concurrent delete cannot slip between check and insert.
        """
        state = "suspended" if suspended else "active"
        insert = pg_insert(TenantRow).values(
            host=host, installation_id=installation_id, org=org, org_id=org_id, state=state
        )
        upsert = insert.on_conflict_do_update(
            constraint="uq_tenants_host_inst",
            set_={
                column: case((TenantRow.state == "deleted", current), else_=new)
                for column, current, new in (
                    ("org", TenantRow.org, org),
                    ("org_id", TenantRow.org_id, org_id),
                    ("state", TenantRow.state, state),
                )
            },
        ).returning(TenantRow.id)
        async with self._sf() as session, session.begin():
            current = await self._locked_standing(session, host, installation_id)
            if current is not None and current.state == "deleted":
                raise TenantDeletedError(host, installation_id, "deleted")
            tenant_id: UUID = (await session.execute(upsert)).scalar_one()
        return tenant_id

    @staticmethod
    def deleted_error(
        host: str, installation_id: int, error: DBAPIError
    ) -> TenantDeletedError | None:
        """The `TenantDeletedError` for a `tb_upsert_tenant` refusal, or None for any other error.

        The function raises SQLSTATE `TB001` for a tombstone and `TB002` for a tenant with a
        deletion in progress (migration `3b8d6f2a4c17`).
        """
        reason = _UPSERT_REFUSALS.get(getattr(error.orig, "sqlstate", None) or "")
        return None if reason is None else TenantDeletedError(host, installation_id, reason)

    @staticmethod
    async def _standing(session: AsyncSession, host: str, installation_id: int) -> Row[Any] | None:
        """The tenant row's `(id, state)`, or None; a tombstoned or deleting key is refused.

        Raises `TenantDeletedError` (`tombstone`: a `tenant_tombstones` row for the key, or
        a `database`-step deletion whose tenant row is gone; `deleting`: a non-terminal deletion). A
        tenant whose state is `deleted` is returned, and each caller decides what to do with it.
        """
        tombstone = select(
            or_(
                select(TenantTombstone.host)
                .where(
                    TenantTombstone.host == host,
                    TenantTombstone.installation_id == installation_id,
                )
                .exists(),
                select(TenantDeletion.id)
                .where(
                    TenantDeletion.host == host,
                    TenantDeletion.installation_id == installation_id,
                    TenantDeletion.state == "database",
                    ~select(TenantRow.id)
                    .where(TenantRow.host == host, TenantRow.installation_id == installation_id)
                    .exists(),
                )
                .exists(),
            )
        )
        existing = select(
            TenantRow.id,
            TenantRow.state,
            select(TenantDeletion.id)
            .where(
                TenantDeletion.tenant_id == TenantRow.id,
                TenantDeletion.state.not_in(("done", "failed", "cancelled")),
            )
            .exists()
            .label("deleting"),
        ).where(TenantRow.host == host, TenantRow.installation_id == installation_id)
        if (await session.execute(tombstone)).scalar():
            raise TenantDeletedError(host, installation_id, "tombstone")
        current = (await session.execute(existing)).first()
        if current is not None and current.deleting:
            raise TenantDeletedError(host, installation_id, "deleting")
        return current

    async def _locked_standing(
        self, session: AsyncSession, host: str, installation_id: int
    ) -> Row[Any] | None:
        """Take the installation's advisory lock, then `_standing`.

        The lock is the one `tb_upsert_tenant`, `tb_delete_tenant` and `tb_request_tenant_deletion`
        take first (`tb_tenant:<host>:<installation_id>`, hashed with `hashtextextended`, held to
        the end of the transaction), so a concurrent delete cannot slip between check and write.
        """
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"tb_tenant:{host}:{installation_id}"},
        )
        return await self._standing(session, host, installation_id)

    async def tenant_id_for(self, host: str, installation_id: int) -> UUID | None:
        """The tenant of `(host, installation_id)`, if known."""
        stmt = select(TenantRow.id).where(
            TenantRow.host == host, TenantRow.installation_id == installation_id
        )
        async with self._sf() as session:
            return (await session.execute(stmt)).scalar_one_or_none()

    async def active_tenant_id_for(self, host: str, installation_id: int) -> UUID | None:
        """The tenant of `(host, installation_id)`, if known and not refused.

        Raises `TenantDeletedError` for a tenant that is deleted, being deleted or tombstoned
        (the reasons of `upsert_installation`), so a caller never writes for or schedules it.
        """
        async with self._sf() as session:
            current = await self._standing(session, host, installation_id)
        if current is None:
            return None
        if current.state == "deleted":
            raise TenantDeletedError(host, installation_id, "deleted")
        tenant_id: UUID = current.id
        return tenant_id

    async def set_installation_state(
        self, host: str, installation_id: int, state: str
    ) -> UUID | None:
        """Set the installation state; the tenant id, or None if unknown.

        Under the installation's advisory lock (as `upsert_installation`), and refused with
        `TenantDeletedError` for a tombstoned or deleting key and for a `deleted` tenant, which
        stays as it is. Setting `deleted` on a tenant that is already `deleted` changes nothing
        and returns its id, so a retried delivery whose audit write failed is not refused.
        """
        async with self._sf() as session, session.begin():
            current = await self._locked_standing(session, host, installation_id)
            if current is None:
                return None
            tenant_id: UUID = current.id
            if current.state == "deleted":
                if state != "deleted":
                    raise TenantDeletedError(host, installation_id, "deleted")
                return tenant_id
            await session.execute(
                update(TenantRow).where(TenantRow.id == tenant_id).values(state=state)
            )
        return tenant_id

    async def apply_repository_changes(
        self, tenant_id: UUID, added: Sequence[tuple[int, str]], removed: Sequence[int]
    ) -> None:
        """Upsert the added repositories and delete the removed ones."""
        async with self._sf() as session, session.begin():
            if added:
                ins = pg_insert(TenantRepository).values(
                    [
                        {"tenant_id": tenant_id, "repository_id": rid, "full_name": name}
                        for rid, name in added
                    ]
                )
                await session.execute(
                    ins.on_conflict_do_update(
                        index_elements=["tenant_id", "repository_id"],
                        set_={"full_name": ins.excluded.full_name},
                    )
                )
            if removed:
                await session.execute(
                    delete(TenantRepository).where(
                        TenantRepository.tenant_id == tenant_id,
                        TenantRepository.repository_id.in_(removed),
                    )
                )

    async def rename_repository(self, tenant_id: UUID, repository_id: int, full_name: str) -> None:
        """Record a rename or transfer (identity is the numeric id)."""
        stmt = (
            update(TenantRepository)
            .where(
                TenantRepository.tenant_id == tenant_id,
                TenantRepository.repository_id == repository_id,
            )
            .values(full_name=full_name)
        )
        async with self._sf() as session, session.begin():
            await session.execute(stmt)

    async def _row(self, tenant_id: UUID) -> TenantRow:
        async with self._sf() as session:
            row = await session.get(TenantRow, tenant_id)
        if row is None:
            raise UnknownTenantError(str(tenant_id))
        return row

    async def installation_id(self, tenant_id: UUID) -> int:
        """The installation id of the tenant."""
        return (await self._row(tenant_id)).installation_id

    async def get_tenant(self, tenant_id: UUID) -> Tenant:
        """The adapter-facing tenant."""
        row = await self._row(tenant_id)
        return Tenant(id=row.id, github_host=self._hosts(row.host), org=row.org)


class MemoryTenants:
    """Process-local twin of `SqlTenants` for `MODE=mock` and unit tests."""

    def __init__(self, hosts: HostLookup) -> None:
        """`hosts` maps a host name to its adapter-facing projection."""
        self._hosts = hosts
        self._by_key: dict[tuple[str, int], UUID] = {}
        self._rows: dict[UUID, dict[str, Any]] = {}
        self.repositories: dict[UUID, dict[int, str]] = {}
        self.tombstones: set[tuple[str, int]] = set()  # keys with a `tenant_tombstones` row
        self.deleting: set[tuple[str, int]] = set()  # keys with a non-terminal deletion

    async def upsert_installation(
        self, host: str, installation_id: int, org: str, org_id: int, *, suspended: bool = False
    ) -> UUID:
        """Create or update the tenant as `suspended` or `active`.

        Mirrors `SqlTenants`: a tombstoned key, a key being deleted and a `deleted` tenant raise
        `TenantDeletedError`.
        """
        key = (host, installation_id)
        if key in self.tombstones:
            raise TenantDeletedError(host, installation_id, "tombstone")
        if key in self.deleting:
            raise TenantDeletedError(host, installation_id, "deleting")
        known = self._by_key.get(key)
        if known is not None and self._rows[known].get("state") == "deleted":
            raise TenantDeletedError(host, installation_id, "deleted")
        tenant_id = self._by_key.setdefault(key, uuid4())
        self._rows[tenant_id] = {
            "host": host,
            "installation_id": installation_id,
            "org": org,
            "org_id": org_id,
            "state": "suspended" if suspended else "active",
        }
        return tenant_id

    async def tenant_id_for(self, host: str, installation_id: int) -> UUID | None:
        """The tenant of `(host, installation_id)`, if known."""
        return self._by_key.get((host, installation_id))

    async def set_installation_state(
        self, host: str, installation_id: int, state: str
    ) -> UUID | None:
        """Set the installation state; the tenant id, or None if unknown.

        Mirrors `SqlTenants`: a tombstoned or deleting key, and a `deleted` tenant given another
        state, raise `TenantDeletedError`; `deleted` on a `deleted` tenant is a no-op.
        """
        tenant_id = self._standing(host, installation_id)
        if tenant_id is None:
            return None
        if self._rows[tenant_id].get("state") == "deleted" and state != "deleted":
            raise TenantDeletedError(host, installation_id, "deleted")
        self._rows[tenant_id]["state"] = state
        return tenant_id

    def _standing(self, host: str, installation_id: int) -> UUID | None:
        """The tenant of the key if known; a tombstoned or deleting key raises."""
        key = (host, installation_id)
        if key in self.tombstones:
            raise TenantDeletedError(host, installation_id, "tombstone")
        if key in self.deleting:
            raise TenantDeletedError(host, installation_id, "deleting")
        return self._by_key.get(key)

    async def active_tenant_id_for(self, host: str, installation_id: int) -> UUID | None:
        """The tenant of the key if known; raises `TenantDeletedError` as `SqlTenants` does."""
        known = self._standing(host, installation_id)
        if known is not None and self._rows[known].get("state") == "deleted":
            raise TenantDeletedError(host, installation_id, "deleted")
        return known

    async def apply_repository_changes(
        self, tenant_id: UUID, added: Sequence[tuple[int, str]], removed: Sequence[int]
    ) -> None:
        """Upsert the added repositories and delete the removed ones."""
        repos = self.repositories.setdefault(tenant_id, {})
        repos.update(dict(added))
        for rid in removed:
            repos.pop(rid, None)

    async def rename_repository(self, tenant_id: UUID, repository_id: int, full_name: str) -> None:
        """Record a rename or transfer."""
        repos = self.repositories.setdefault(tenant_id, {})
        if repository_id in repos:
            repos[repository_id] = full_name

    async def installation_id(self, tenant_id: UUID) -> int:
        """The installation id of the tenant."""
        row = self._rows.get(tenant_id)
        if row is None:
            raise UnknownTenantError(str(tenant_id))
        return int(row["installation_id"])

    async def get_tenant(self, tenant_id: UUID) -> Tenant:
        """The adapter-facing tenant."""
        row = self._rows.get(tenant_id)
        if row is None:
            raise UnknownTenantError(str(tenant_id))
        return Tenant(id=tenant_id, github_host=self._hosts(row["host"]), org=row["org"])


class ParkedValues:
    """`ValueStore` over the secret store: one JSON entry per operation."""

    def __init__(self, secrets: WritableSecrets) -> None:
        """Use `secrets` (a writable store; the read-only schemes cannot hold values)."""
        self._secrets = secrets

    @staticmethod
    def _ref(operation_id: UUID) -> str:
        return f"store:{PARKED_PREFIX}{operation_id}"

    async def put(self, operation_id: UUID, secret: IssuedSecret) -> None:
        """Park the values of an operation."""
        document = {
            "values": {k: v.get_secret_value() for k, v in secret.values.items()},
            "expires_at": secret.expires_at.isoformat() if secret.expires_at else None,
        }
        await self._secrets.write(self._ref(operation_id), SecretStr(json.dumps(document)))

    async def get(self, operation_id: UUID) -> IssuedSecret | None:
        """The parked values, or None when nothing is parked for the operation."""
        try:
            raw = await self._secrets.read(self._ref(operation_id))
        except SecretUnavailable:
            return None
        document = json.loads(raw.get_secret_value())
        expires = document["expires_at"]
        return IssuedSecret(
            values={k: SecretStr(v) for k, v in document["values"].items()},
            expires_at=datetime.fromisoformat(expires) if expires else None,
        )

    async def delete(self, operation_id: UUID) -> None:
        """Remove the parked values; absent is fine."""
        await self._secrets.delete(self._ref(operation_id))

    async def list_ids(self) -> Sequence[UUID]:
        """The operation ids that have parked values."""
        refs = await self._secrets.list_refs(PARKED_PREFIX)
        return [UUID(ref.rsplit("/", 1)[1]) for ref in refs]


class ConnectionData:
    """A tenant's per-adapter connection data in the secret store."""

    def __init__(self, secrets: WritableSecrets) -> None:
        """Use `secrets` for the JSON object of each `(tenant, adapter)`."""
        self._secrets = secrets
        # Per-ref locks, dropped once nobody holds them. The secret store has no compare-and-set,
        # so this serialises read-modify-write within one process only.
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    @staticmethod
    def _ref(tenant_id: UUID, adapter: str) -> str:
        return f"store:tenants/{tenant_id}/{adapter}"

    def _lock(self, ref: str) -> asyncio.Lock:
        lock = self._locks.get(ref)
        if lock is None:
            lock = self._locks[ref] = asyncio.Lock()
        return lock

    async def _load(self, tenant_id: UUID, adapter: str) -> dict[str, str]:
        """The stored object; `{}` only when it is absent. An outage propagates.

        Treating an outage as "absent" would let `put` overwrite the stored blob with a
        partial one and lose data.
        """
        try:
            raw = await self._secrets.read(self._ref(tenant_id, adapter))
        except SecretNotFound:
            return {}
        loaded: dict[str, str] = json.loads(raw.get_secret_value())
        return loaded

    async def put(self, tenant_id: UUID, adapter: str, values: Mapping[str, SecretStr]) -> None:
        """Merge `values` into the stored object (last writer wins per key)."""
        ref = self._ref(tenant_id, adapter)
        async with self._lock(ref):
            merged = await self._load(tenant_id, adapter)
            merged.update({k: v.get_secret_value() for k, v in values.items()})
            await self._secrets.write(ref, SecretStr(json.dumps(merged)))

    async def config_for(self, tenant: Tenant, adapter: str) -> AdapterConfig:
        """The adapter's config; a tenant with no data gets an empty one."""
        data = await self._load(tenant.id, adapter)
        privileged = data.pop(PRIVILEGED_KEY, None)
        return AdapterConfig(
            tenant=tenant,
            connection={k: SecretStr(v) for k, v in data.items()},
            privileged_credential=SecretStr(privileged) if privileged else None,
        )


class ReconcileRequests:
    """`ReconcileScheduler`: remember which tenants want a reconcile soon."""

    def __init__(self, redis: Redis | None = None) -> None:
        """Without Valkey (mock mode) requests are kept in this process."""
        self._redis = redis
        self.local: set[str] = set()

    async def schedule(self, tenant_id: UUID) -> None:
        """Record the request (a set: scheduling twice is one request)."""
        if self._redis is None:
            self.local.add(str(tenant_id))
            return
        await self._redis.sadd(RECONCILE_SET, str(tenant_id))
