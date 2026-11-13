"""SQLAlchemy tables NG3 owns, on the template's shared `Base` (one Alembic history).

`webhook_deliveries` (the outbox and dedupe record), `scope_leases` (fenced leases),
`tenants` (surrogate tenant id for `(host, installation_id)`) and `schema_compat` (the
single-row schema-compatibility integer). Columns are NG3's within ADR 0028's invariants.
The hand-written revision in `alembic/versions/` is the schema of record; a drift test
compares it to these models.
"""

import uuid
from datetime import datetime

from craftainer_crud.models.base import Base
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

NIL_TENANT = uuid.UUID(int=0)
"""Tenant id used for core-wide (non-tenant) lease keys, such as the reconciler's duties."""


class WebhookDelivery(Base):
    """One received webhook delivery: the transactional outbox and the dedupe record.

    `body_sha256` (hex SHA-256 of the raw signed body) is unique per host: `X-GitHub-Delivery` is
    not signed, so the same captured body replayed under a new delivery id is a duplicate.
    The primary key is `(host, delivery_id)`: GitHub's delivery GUID is unique per host, and a
    host must not be able to suppress another host's delivery by reusing its id.
    Host-scoped and outside tenant RLS (written before the tenant is known). `status` is
    `pending`, `processed`, `dead` or `deleted` (a tenant's body cleared by its data deletion,
    ADR 0034; nothing relays or handles such a row again); `attempts` counts handler failures
    only. `tenant_id` is set by the worker when it resolves the delivery's installation (null
    until then and for rows older than the column); it has no foreign key.
    """

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        CheckConstraint(
            "status in ('pending', 'processed', 'dead', 'deleted')", name="ck_wd_status"
        ),
        Index("ix_wd_status_next", "status", "next_attempt_at"),
        Index("ix_wd_host_status", "host", "status"),
        Index("ix_wd_received_at", "received_at"),
        Index("ux_wd_host_body_sha256", "host", "body_sha256", unique=True),
        Index("ix_wd_tenant", "tenant_id", postgresql_where=text("tenant_id IS NOT NULL")),
    )

    host: Mapped[str] = mapped_column(String, primary_key=True)
    delivery_id: Mapped[str] = mapped_column(String, primary_key=True)
    event: Mapped[str] = mapped_column(String)
    headers: Mapped[dict[str, str]] = mapped_column(JSONB)
    body: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    status: Mapped[str] = mapped_column(String, server_default="pending")
    attempts: Mapped[int] = mapped_column(BigInteger, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_relayed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dead_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    body_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class ScopeLease(Base):
    """A lease on `(tenant, adapter, lease key)` with a fencing counter (not an advisory lock)."""

    __tablename__ = "scope_leases"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    adapter: Mapped[str] = mapped_column(String, primary_key=True)
    lease_key: Mapped[str] = mapped_column(String, primary_key=True)
    holder: Mapped[str] = mapped_column(String)
    fence: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Tenant(Base):
    """A tenant: one GitHub installation on one host (ADR 0028 "Tenancy")."""

    __tablename__ = "tenants"
    __table_args__ = (UniqueConstraint("host", "installation_id", name="uq_tenants_host_inst"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    host: Mapped[str] = mapped_column(String)
    installation_id: Mapped[int] = mapped_column(BigInteger)
    # Filled from `installation` events (a tenant first seen from another event has none yet).
    org: Mapped[str] = mapped_column(String, server_default="")
    org_id: Mapped[int] = mapped_column(BigInteger, server_default="0")
    # `active`, `suspended` or `deleted`.
    state: Mapped[str] = mapped_column(String, server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Set together by `tb_set_tenant_disabled` (ADR 0033); the sync never writes them.
    admin_disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    admin_disabled_by: Mapped[str | None] = mapped_column(Text)
    admin_disable_reason: Mapped[str | None] = mapped_column(Text)


DELETION_TERMINAL_STATES = ("done", "failed", "cancelled")
"""The `tenant_deletions.state` values after which a deletion is over (ADR 0034)."""


class TenantDeletion(Base):
    """One tenant data deletion (ADR 0034): a write-ahead, resumable operation.

    Host-scoped and outside RLS, with no foreign key to `tenants`: the record outlives the
    tenant row. It stores ids, flags, counts and what was skipped, never the organisation login
    or other tenant content. At most one non-terminal row exists per tenant.
    """

    __tablename__ = "tenant_deletions"
    __table_args__ = (
        CheckConstraint(
            "state in ('requested', 'draining', 'platform', 'github', 'database', "
            "'done', 'failed', 'cancelled')",
            name="ck_tenant_deletions_state",
        ),
        Index(
            "ux_tenant_deletions_open",
            "tenant_id",
            unique=True,
            postgresql_where=text("state NOT IN ('done', 'failed', 'cancelled')"),
        ),
        Index("ix_tenant_deletions_state_updated", "state", "updated_at"),
        Index(
            "ix_tenant_deletions_done_key",
            "host",
            "installation_id",
            postgresql_where=text("state = 'done'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    host: Mapped[str] = mapped_column(Text)
    installation_id: Mapped[int] = mapped_column(BigInteger)
    requested_by_sub: Mapped[str] = mapped_column(Text)
    requested_by_iss: Mapped[str] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    delete_platform_resources: Mapped[bool] = mapped_column(server_default=text("false"))
    state: Mapped[str] = mapped_column(Text, server_default="requested")
    step_cursor: Mapped[dict[str, object]] = mapped_column(JSONB, server_default=text("'{}'"))
    counts: Mapped[dict[str, object]] = mapped_column(JSONB, server_default=text("'{}'"))
    skipped: Mapped[list[object]] = mapped_column(JSONB, server_default=text("'[]'"))
    holder: Mapped[str | None] = mapped_column(Text)
    fence: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_error: Mapped[str | None] = mapped_column(Text)


class TenantRepository(Base):
    """A repository the tenant's installation can see (identity is the numeric id)."""

    __tablename__ = "tenant_repositories"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    repository_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    full_name: Mapped[str] = mapped_column(String)


class SchemaCompat(Base):
    """The schema-compatibility integer: one row, written by every migration."""

    __tablename__ = "schema_compat"
    __table_args__ = (CheckConstraint("id = 1", name="ck_schema_compat_single_row"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    version: Mapped[int] = mapped_column(BigInteger)
