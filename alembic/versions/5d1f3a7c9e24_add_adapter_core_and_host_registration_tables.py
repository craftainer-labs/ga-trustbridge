"""add adapter core tables and the GitHub host registration table (NG4)

Revision ID: 5d1f3a7c9e24
Revises: c3a5e7d91f42
Create Date: 2026-11-01 12:00:00.000000

Tables of ADR 0028 owned by NG4: adapter_resources, adapter_credentials,
credential_operations; plus github_host_registrations (ADR 0030, where the manifest
flow or manual registration stores a host's App id and secret references).
It follows NG3's migration (NG34 integrated the two): it also adds the tenant columns the
installation lifecycle handlers fill (``org``, ``org_id``, ``state``), the repositories table,
the runtime role's rights, and the schema-compatibility write every migration makes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5d1f3a7c9e24"
down_revision: str | Sequence[str] | None = "c3a5e7d91f42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SCHEMA_COMPAT_VERSION = 1
RUNTIME_ROLE = "trustbridge_app"
NG4_TABLES = (
    "adapter_resources, adapter_credentials, credential_operations, "
    "github_host_registrations, tenant_repositories"
)


def grant_runtime_access(role: str = RUNTIME_ROLE) -> None:
    """Grant the runtime role its rights on this revision's tables, if the role exists."""
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON {NG4_TABLES} TO {role};
            END IF;
        END
        $$
        """  # noqa: S608 -- constants and ints only
    )


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


    op.add_column("tenants", sa.Column("org", sa.String(length=255), server_default="", nullable=False))
    op.add_column("tenants", sa.Column("org_id", sa.BigInteger(), server_default="0", nullable=False))
    op.add_column(
        "tenants", sa.Column("state", sa.String(length=16), server_default="active", nullable=False)
    )
    op.create_table(
        "tenant_repositories",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("repository_id", sa.BigInteger(), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "repository_id"),
    )
    op.execute(
        "INSERT INTO schema_compat (id, version) VALUES (1, "  # noqa: S608 -- constants and ints only
        f"{SCHEMA_COMPAT_VERSION}) ON CONFLICT (id) DO UPDATE SET version = "
        f"GREATEST(schema_compat.version, {SCHEMA_COMPAT_VERSION})"
    )
    grant_runtime_access()


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("tenant_repositories")
    op.drop_column("tenants", "state")
    op.drop_column("tenants", "org_id")
    op.drop_column("tenants", "org")
    op.drop_table("github_host_registrations")
    op.drop_index("uq_credential_operations_open_scope", table_name="credential_operations")
    op.drop_index(op.f("ix_credential_operations_tenant_id"), table_name="credential_operations")
    op.drop_table("credential_operations")
    op.drop_table("adapter_credentials")
    op.drop_table("adapter_resources")
