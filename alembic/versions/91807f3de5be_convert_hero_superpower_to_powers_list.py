"""convert hero superpower to powers list

Revision ID: 91807f3de5be
Revises: 7dc8146fcf6c
Create Date: 2026-09-04 07:13:14.611387

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "91807f3de5be"
down_revision: str | Sequence[str] | None = "7dc8146fcf6c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("heroes", sa.Column("powers", postgresql.ARRAY(sa.String()), nullable=True))
    op.execute("UPDATE heroes SET powers = ARRAY[superpower]")
    op.alter_column("heroes", "powers", nullable=False)
    op.drop_column("heroes", "superpower")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column("heroes", sa.Column("superpower", sa.String(), nullable=True))
    op.execute("UPDATE heroes SET superpower = powers[1]")
    op.alter_column("heroes", "superpower", nullable=False)
    op.drop_column("heroes", "powers")
