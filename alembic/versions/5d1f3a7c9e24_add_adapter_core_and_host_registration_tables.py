"""add adapter core tables and the GitHub host registration table (NG4)

Revision ID: 5d1f3a7c9e24
Revises: 49e3e2487a13
Create Date: 2026-11-01 12:00:00.000000

Tables of ADR 0028 owned by NG4: adapter_resources, adapter_credentials,
credential_operations; plus github_host_registrations (ADR 0030, where the manifest
flow or manual registration stores a host's App id and secret references).
The lander rebases down_revision onto NG3's migration if it lands first, and adds
the schema-compatibility write NG3 defines for every migration.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5d1f3a7c9e24"
down_revision: str | Sequence[str] | None = "49e3e2487a13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "adapter_resources",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("adapter", sa.String(), nullable=False),
        sa.Column("resource_key", sa.String(), nullable=False),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tenant_id", "adapter", "resource_key"),
    )
    op.create_table(
        "adapter_credentials",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("adapter", sa.String(), nullable=False),
        sa.Column("scope_key", sa.String(), nullable=False),
        sa.Column("scope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("kind", sa.String(), nullable=True),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "outputs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tenant_id", "adapter", "scope_key"),
    )
    op.create_table(
        "credential_operations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("adapter", sa.String(), nullable=False),
        sa.Column("scope_key", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("oidc_offered", sa.Boolean(), nullable=False),
        sa.Column("base_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("base_kind", sa.String(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("manual_action", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_credential_operations_tenant_id"), "credential_operations", ["tenant_id"]
    )
    op.create_index(
        "uq_credential_operations_open_scope",
        "credential_operations",
        ["tenant_id", "adapter", "scope_key"],
        unique=True,
        postgresql_where=sa.text("status NOT IN ('finished', 'failed', 'abandoned')"),
    )
    op.create_table(
        "github_host_registrations",
        sa.Column("host_name", sa.String(), nullable=False),
        sa.Column("app_id", sa.BigInteger(), nullable=False),
        sa.Column("webhook_secret_ref", sa.String(), nullable=False),
        sa.Column("app_slug", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("host_name"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("github_host_registrations")
    op.drop_index("uq_credential_operations_open_scope", table_name="credential_operations")
    op.drop_index(op.f("ix_credential_operations_tenant_id"), table_name="credential_operations")
    op.drop_table("credential_operations")
    op.drop_table("adapter_credentials")
    op.drop_table("adapter_resources")
