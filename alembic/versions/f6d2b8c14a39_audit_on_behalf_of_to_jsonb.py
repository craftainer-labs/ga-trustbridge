"""change audit_events.on_behalf_of from text to jsonb (NG6f, ADR 0032)

Revision ID: f6d2b8c14a39
Revises: e5c9a37d2b18
Create Date: 2026-11-04 14:00:00.000000

The column carries the webhook sender and delivery as a JSON object
(``{"github_sender_id": <int>, "delivery_id": "<str>"}``) instead of a ``key=value`` string
(user decision, NG6f).

N-1 compatible (NFR-0027): the previous release never touches ``audit_events`` (the table was
created for this release, see the previous revision), and until this release nothing wrote
``on_behalf_of`` (the purge summary leaves it null). So there is no old reader or writer to keep
working and no data to backfill in batches; a single in-place type change is the expand step and
there is no contract step. The change rewrites the table, which holds only purge summaries at
this point; it takes the exclusive lock briefly and lock and statement timeouts are set first.
The row triggers do not fire on a rewrite, so append-only enforcement is untouched.

A value that is somehow present (a dev database; no release wrote one) is converted, not
dropped, when it has the old ``sender=<digits>;delivery=<id>`` form: it becomes the object. Any
other stray value cannot be represented and becomes null. No roles are created and no grants
change: the existing table grants carry over.

``ck_audit_events_on_behalf_of`` then pins the shape in the database, so no insert path (the app
role's ``INSERT`` grant included) can store anything but null or
``{"github_sender_id": <number>, "delivery_id": "<string>"}``. It is added ``NOT VALID`` and then
validated in the same step (Squawk's ``constraint-missing-not-valid``); the table is tiny and the
conversion above guarantees every existing row passes. The downgrade drops it first.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6d2b8c14a39"
down_revision: str | Sequence[str] | None = "e5c9a37d2b18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CHECK_NAME = "ck_audit_events_on_behalf_of"


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    # Squawk's changing-column-type is ignored for this one statement: see the module docstring
    # (the previous release never touches the table and nothing has written the column yet).
    op.execute(
        "-- squawk-ignore changing-column-type\n"
        "ALTER TABLE audit_events ALTER COLUMN on_behalf_of TYPE jsonb "
        "USING CASE WHEN on_behalf_of ~ '^sender=[0-9]{1,18};delivery=' THEN "
        "jsonb_build_object('github_sender_id', "
        "CAST(substring(on_behalf_of FROM '^sender=([0-9]+);') AS bigint), "
        "'delivery_id', substring(on_behalf_of FROM '^sender=[0-9]+;delivery=(.*)$')) "
        "ELSE NULL END"
    )
    op.execute(
        f"ALTER TABLE audit_events ADD CONSTRAINT {CHECK_NAME} CHECK ("
        "on_behalf_of IS NULL OR ("
        "jsonb_typeof(on_behalf_of) = 'object' "
        "AND on_behalf_of ?& array['github_sender_id', 'delivery_id'] "
        "AND jsonb_typeof(on_behalf_of->'github_sender_id') = 'number' "
        "AND jsonb_typeof(on_behalf_of->'delivery_id') = 'string')) NOT VALID"
    )
    op.execute(f"ALTER TABLE audit_events VALIDATE CONSTRAINT {CHECK_NAME}")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute(f"ALTER TABLE audit_events DROP CONSTRAINT IF EXISTS {CHECK_NAME}")
    op.execute(
        "-- squawk-ignore changing-column-type\n"
        "ALTER TABLE audit_events ALTER COLUMN on_behalf_of TYPE text "
        "USING CASE WHEN on_behalf_of IS NULL THEN NULL "
        "WHEN jsonb_typeof(on_behalf_of) = 'string' THEN on_behalf_of #>> '{}' "
        "ELSE on_behalf_of::text END"
    )
