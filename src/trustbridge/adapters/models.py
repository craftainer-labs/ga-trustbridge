"""SQLAlchemy tables for the adapter core (ADR 0028): resources, credentials, operations.

The tables share `craftainer_crud.models.base.Base` so the single Alembic history sees them
(import this module before autogenerate). `tenant_id` carries no foreign key here: the
`tenants` table belongs to NG3, and RLS (ADR 0031) is applied by NG6's migration.
"""

from datetime import datetime
from uuid import UUID

from craftainer_crud.models.base import Base
from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column


class AdapterResourceRow(Base):
    """`ResourceState` of a non-secret resource."""

    __tablename__ = "adapter_resources"

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    adapter: Mapped[str] = mapped_column(String, primary_key=True)
    resource_key: Mapped[str] = mapped_column(String, primary_key=True)
    state: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AdapterCredentialRow(Base):
    """`CredentialState` per credential scope. Holds names and platform refs, never secrets."""

    __tablename__ = "adapter_credentials"

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    adapter: Mapped[str] = mapped_column(String, primary_key=True)
    scope_key: Mapped[str] = mapped_column(String, primary_key=True)
    scope: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    kind: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    outputs: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CredentialOperationRow(Base):
    """Write-ahead log of secret-bearing operations. At most one non-terminal row per scope."""

    __tablename__ = "credential_operations"
    __table_args__ = (
        Index(
            "uq_credential_operations_open_scope",
            "tenant_id",
            "adapter",
            "scope_key",
            unique=True,
            postgresql_where=text("status NOT IN ('finished', 'failed', 'abandoned')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    adapter: Mapped[str] = mapped_column(String, nullable=False)
    scope_key: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    oidc_offered: Mapped[bool] = mapped_column(Boolean, nullable=False)
    base_state: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    base_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = ["AdapterCredentialRow", "AdapterResourceRow", "CredentialOperationRow"]
