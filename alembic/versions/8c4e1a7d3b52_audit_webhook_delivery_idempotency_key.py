"""unique audit idempotency key for webhook-driven events (NG6h-2b, ADR 0032)

Revision ID: 8c4e1a7d3b52
Revises: 6f3b9d2e4a85
Create Date: 2026-11-13 15:00:00.000000

A redelivered webhook must not write a second audit row. ``on_behalf_of`` carries
``{"github_sender_id", "delivery_id"}`` for every webhook-driven event (and only for those), so the
key is ``(tenant_id, action, on_behalf_of->>'delivery_id')`` over the rows where ``on_behalf_of IS
NOT NULL``: a partial unique expression index. The writer (``audit.store.append``) inserts with
``ON CONFLICT (...) WHERE on_behalf_of IS NOT NULL DO NOTHING``, whose target repeats this
definition exactly (PostgreSQL's unique index inference, INSERT docs).

Why an index and not a generated column: ``ADD COLUMN ... GENERATED ALWAYS AS (...) STORED``
rewrites the table under ACCESS EXCLUSIVE (PostgreSQL ALTER TABLE docs), while a
``CREATE UNIQUE INDEX
CONCURRENTLY`` takes only SHARE UPDATE EXCLUSIVE and lets writes continue. No column is added, so
the previous release's INSERT statements (which name their columns) keep working. N-1 compatible
(NFR-0027): an old writer can still insert. One without ``ON CONFLICT`` that collides with an
existing row now gets a unique violation, which fails that handler and the worker retries the
delivery (the retry then lands on a replica that deduplicates or on the same failure until the
rollout ends); before this index it would have written a duplicate row. The window is the
rollout only.

Existing duplicates. The audit log is append-only and its history is never rewritten here. Before
building anything the upgrade counts the key's duplicate groups; if there are any it raises and
changes nothing, naming the count and the query that lists them. An operator then decides how
to reconcile (for instance a reviewed, owner-run change recorded outside the migration). A race
that adds a duplicate between that check and the build makes the concurrent build fail and leave an
INVALID index, which a re-run drops and rebuilds (the same convention as ``a7e2c94b1d63``).

Webhook events with a null ``tenant_id`` are not covered (NULLs are distinct in a unique index);
no emitter writes one. An event whose payload has no numeric sender has a null ``on_behalf_of``
(the CHECK requires a numeric ``github_sender_id``), so it is not deduplicated either; installation
events always carry a sender.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8c4e1a7d3b52"
down_revision: str | Sequence[str] | None = "6f3b9d2e4a85"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ux_audit_events_webhook_delivery"
KEY = "tenant_id, action, (on_behalf_of->>'delivery_id')"
PREDICATE = "on_behalf_of IS NOT NULL"
_DUPLICATES = (
    "SELECT tenant_id, action, (on_behalf_of->>'delivery_id') AS delivery_id, count(*) AS n "
    "FROM audit_events WHERE on_behalf_of IS NOT NULL GROUP BY tenant_id, action, "
    "(on_behalf_of->>'delivery_id') HAVING count(*) > 1"
)


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


def _refuse_existing_duplicates() -> None:
    """Raise, changing nothing, when the table already holds rows the key would reject."""
    if op.get_context().as_sql:
        return
    groups = op.get_bind().execute(sa.text(_DUPLICATES)).all()
    if groups:
        sample = ", ".join(
            f"({g.tenant_id}, {g.action}, {g.delivery_id}) x{g.n}" for g in groups[:5]
        )
        msg = (
            f"audit_events holds {len(groups)} duplicate webhook delivery group(s), so {INDEX} "
            "cannot be built. The audit log is append-only and this migration does not delete or "
            f"edit history. First groups (tenant, action, delivery): {sample}. List them all with: "
            f"{_DUPLICATES}. Reconcile them as the table owner with a reviewed change, then run "
            "the upgrade again."
        )
        raise RuntimeError(msg)


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("SET lock_timeout = '5s'")
    op.execute("SET statement_timeout = '120s'")
    _refuse_existing_duplicates()
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = '600s'")
        _drop_if_invalid()
        op.execute(
            f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} "
            f"ON audit_events ({KEY}) WHERE {PREDICATE}"
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("SET lock_timeout = '5s'")
    op.execute("SET statement_timeout = '120s'")
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
