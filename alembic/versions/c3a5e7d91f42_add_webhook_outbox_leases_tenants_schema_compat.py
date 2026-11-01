"""add webhook outbox, scope leases, tenants and the schema-compatibility table (NG3)

Revision ID: c3a5e7d91f42
Revises: 49e3e2487a13
Create Date: 2026-11-01 12:00:00.000000

Tables: ``webhook_deliveries`` (ingest outbox and dedupe record), ``scope_leases`` (fenced
leases), ``tenants`` ((host, installation_id) to surrogate tenant id) and ``schema_compat``
(the single-row schema-compatibility integer every migration writes with GREATEST, ADR 0028
invariants 12-13). The runtime role ``trustbridge_app`` (created by the owner role's setup,
ADR 0031) gets exactly the rights it needs, only if it exists.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3a5e7d91f42"
down_revision: str | Sequence[str] | None = "49e3e2487a13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_COMPAT_VERSION = 1
RUNTIME_ROLE = "trustbridge_app"


def grant_runtime_access(role: str = RUNTIME_ROLE) -> None:
    """Grant the runtime role its rights on NG3's tables, if the role exists."""
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                GRANT SELECT ON schema_compat TO {role};
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON webhook_deliveries, scope_leases, tenants TO {role};
            END IF;
        END
        $$
        """  # noqa: S608 -- constants and ints only
    )


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "webhook_deliveries",
        sa.Column("delivery_id", sa.String(length=128), nullable=False),
        sa.Column("host", sa.String(length=63), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("headers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("body", sa.LargeBinary(), nullable=True),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_relayed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dead_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint("status in ('pending', 'processed', 'dead')", name="ck_wd_status"),
        sa.PrimaryKeyConstraint("delivery_id"),
    )
    op.create_index("ix_wd_status_next", "webhook_deliveries", ["status", "next_attempt_at"])
    op.create_index("ix_wd_host_status", "webhook_deliveries", ["host", "status"])
    op.create_index("ix_wd_received_at", "webhook_deliveries", ["received_at"])

    op.create_table(
        "scope_leases",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("adapter", sa.String(length=64), nullable=False),
        sa.Column("lease_key", sa.String(length=256), nullable=False),
        sa.Column("holder", sa.String(length=128), nullable=False),
        sa.Column("fence", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "adapter", "lease_key"),
    )

    op.create_table(
        "tenants",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("host", sa.String(length=63), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("host", "installation_id", name="uq_tenants_host_inst"),
    )

    op.create_table(
        "schema_compat",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_schema_compat_single_row"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "INSERT INTO schema_compat (id, version) VALUES (1, "  # noqa: S608 -- constants and ints only
        f"{SCHEMA_COMPAT_VERSION}) ON CONFLICT (id) DO UPDATE SET version = "
        f"GREATEST(schema_compat.version, {SCHEMA_COMPAT_VERSION})"
    )
    grant_runtime_access()


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("schema_compat")
    op.drop_table("tenants")
    op.drop_table("scope_leases")
    op.drop_index("ix_wd_received_at", table_name="webhook_deliveries")
    op.drop_index("ix_wd_host_status", table_name="webhook_deliveries")
    op.drop_index("ix_wd_status_next", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
