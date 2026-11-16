"""internal idempotency key for audit events written at least once (NG5r-d, ADR 0032)

Revision ID: 3d7f1b9c5e82
Revises: 2c8e5b1f7d34
Create Date: 2026-11-16 18:00:00.000000

A writer that must not lose an audit event (the reconcile duty's terminal event) writes it
before acknowledging its work and retries after a failure, so the same event can arrive twice.
``event_key`` is the key that makes the second write a no-op: a nullable ``text`` column and a
partial unique index over ``(tenant_id, action, event_key) WHERE event_key IS NOT NULL``. The
writer (``audit.store.append``) inserts with ``ON CONFLICT (tenant_id, action, event_key) WHERE
event_key IS NOT NULL DO NOTHING``, whose target repeats the index definition exactly.

The column is internal: ``tb_audit_list`` and ``tb_audit_get`` return ``SETOF audit_events`` (they
select ``a.*``), so they carry it, but the store's read statements name their columns and the
export never includes it.

Expand only, N-1 compatible (NFR-0027). ``ADD COLUMN`` of a nullable column without a default is a
catalog change (no rewrite, a brief ACCESS EXCLUSIVE lock under ``lock_timeout``); the index is
built ``CONCURRENTLY`` (SHARE UPDATE EXCLUSIVE, writes continue). The previous release's INSERT
statements name their columns and never set the key, so they keep working, and every existing row
has a null key, which the partial index does not cover, so no existing data can violate it. The
table's grants are table-level, so the app role can insert and select the new column.

An INVALID index left by a failed concurrent build is dropped and rebuilt on a re-run (the same
convention as ``8c4e1a7d3b52``).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3d7f1b9c5e82"
down_revision: str | Sequence[str] | None = "2c8e5b1f7d34"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ux_audit_events_event_key"
KEY = "tenant_id, action, event_key"
PREDICATE = "event_key IS NOT NULL"


def _drop_if_invalid() -> None:
    """Drop an INVALID index left by a failed concurrent build, so a retry rebuilds it."""
    if op.get_context().as_sql:  # offline rendering (the SQL lint) cannot query the catalog
        return
    invalid = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = :name AND NOT i.indisvalid"
            ),
            {"name": INDEX},
        )
        .first()
    )
    if invalid:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("SET lock_timeout = '5s'")
    op.execute("SET statement_timeout = '120s'")
    op.execute("ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS event_key text")
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = '600s'")
        _drop_if_invalid()
        op.execute(
            f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} "
            f"ON audit_events ({KEY}) WHERE {PREDICATE}"
        )
        op.execute("RESET statement_timeout")
        op.execute("RESET lock_timeout")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("SET lock_timeout = '5s'")
    op.execute("SET statement_timeout = '120s'")
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
        op.execute("RESET statement_timeout")
        op.execute("RESET lock_timeout")
    op.execute("ALTER TABLE audit_events DROP COLUMN IF EXISTS event_key")
