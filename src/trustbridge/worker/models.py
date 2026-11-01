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

    Host-scoped and outside tenant RLS (written before the tenant is known). `status` is
    `pending`, `processed` or `dead`; `attempts` counts handler failures only.
    """

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        CheckConstraint("status in ('pending', 'processed', 'dead')", name="ck_wd_status"),
        Index("ix_wd_status_next", "status", "next_attempt_at"),
        Index("ix_wd_host_status", "host", "status"),
        Index("ix_wd_received_at", "received_at"),
    )

    delivery_id: Mapped[str] = mapped_column(String, primary_key=True)
    host: Mapped[str] = mapped_column(String)
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
