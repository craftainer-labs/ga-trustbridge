"""scope webhook delivery ids to the host and dedupe by body hash (NG34f)

Revision ID: a7e2c94b1d63
Revises: 5d1f3a7c9e24
Create Date: 2026-11-02 12:00:00.000000

``webhook_deliveries`` was keyed by ``delivery_id`` alone. GitHub's delivery GUID is unique per
GitHub host, not across hosts, so the key becomes ``(host, delivery_id)``: one host can no
longer suppress another host's delivery by reusing its id, and two hosts' ids never collide.

It also adds ``body_sha256`` (SHA-256 of the raw signed body, hex) with a unique index on
``(host, body_sha256)``: ``X-GitHub-Delivery`` is not covered by the HMAC signature, so a
captured body could otherwise be replayed under a new delivery id. A row lives for the dedupe
window (``delivery_id_retention_days``), so the same body is accepted once per window. Rows
that predate the column keep NULL (NULLs never conflict).

Lock-safe: the column is nullable without a default (a catalog-only change), the unique
indexes are built ``CONCURRENTLY`` (outside the migration transaction), then one
``ALTER TABLE`` swaps the primary key onto the first (``USING INDEX``), which only takes a
brief lock and builds nothing. ``host`` is already ``NOT NULL``. The pre-release code that inserts
with ``ON CONFLICT (delivery_id)`` must be replaced together with this migration; the migration
runs in the dedicated migrate job before the rollout (ADR 0028).
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7e2c94b1d63"
down_revision: str | Sequence[str] | None = "5d1f3a7c9e24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("SET lock_timeout = '5s'")
    op.execute("SET statement_timeout = '120s'")
    op.execute("ALTER TABLE webhook_deliveries ADD COLUMN IF NOT EXISTS body_sha256 text")
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ux_wd_host_delivery "
            "ON webhook_deliveries (host, delivery_id)"
        )
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ux_wd_host_body_sha256 "
            "ON webhook_deliveries (host, body_sha256)"
        )
    op.execute(
        "ALTER TABLE webhook_deliveries DROP CONSTRAINT webhook_deliveries_pkey, "
        "ADD CONSTRAINT webhook_deliveries_pkey PRIMARY KEY USING INDEX ux_wd_host_delivery"
    )


def downgrade() -> None:
    """Downgrade schema (fails if two hosts already share a delivery id)."""
    op.execute("SET lock_timeout = '5s'")
    op.execute("SET statement_timeout = '120s'")
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ux_wd_host_body_sha256")
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ux_wd_delivery "
            "ON webhook_deliveries (delivery_id)"
        )
    op.execute(
        "ALTER TABLE webhook_deliveries DROP CONSTRAINT webhook_deliveries_pkey, "
        "ADD CONSTRAINT webhook_deliveries_pkey PRIMARY KEY USING INDEX ux_wd_delivery"
    )
    op.execute("ALTER TABLE webhook_deliveries DROP COLUMN IF EXISTS body_sha256")
