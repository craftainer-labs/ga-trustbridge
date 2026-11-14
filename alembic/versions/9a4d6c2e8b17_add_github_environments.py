"""add github_environments: the environments trustbridge created, tracked per tenant (NG6h-6a)

Revision ID: 9a4d6c2e8b17
Revises: 7b2d4f8a1c36
Create Date: 2026-11-14 09:00:00.000000

ADR 0034 user decision B6: a tenant deletion removes only the GitHub environments trustbridge
created, never one it found already there (an environment carries protection rules and may hold
secrets of its own). Until now nothing recorded who created an environment. The GitHub writer now
writes one row here when its `PUT /repos/{full}/environments/{name}` created the environment
(`GET` answered 404 first), and none when the environment already existed. NG6h-6 deletes exactly
the tracked environments and removes their rows; an environment with no row is kept and recorded
as `environment_kept`.

* ``github_environments (tenant_id, host, repository_id, environment_name, created_at)``, primary
  key ``(tenant_id, repository_id, environment_name)``. No foreign key (the tenant tables have
  none; the deletion's database step clears the rows). Ids and the environment's name only, which
  is already in the adapters' recorded outputs.
* Tenant-scoped like the other tenant tables: the ``tenant_isolation`` policy (``FOR ALL``, the
  same predicate and ``WITH CHECK``) for the app role and ``owner_all`` for the owner role, created
  inert (no ``ENABLE``/``FORCE ROW LEVEL SECURITY``: release N+1 does that for every tenant table
  at once, ADR 0031).
* Least privilege: the app role gets ``SELECT, INSERT, DELETE`` and ``UPDATE (created_at)`` only.
  The column grant is what ``INSERT ... ON CONFLICT DO UPDATE`` (a repeat creation refreshes the
  time) and the deletion step's ``SELECT ... FOR UPDATE SKIP LOCKED`` batches need (a row lock
  requires ``UPDATE`` privilege on at least one column); no other column can change.

Expand-only (NFR-0027): one new table, no existing object touched, no backfill (what an earlier
release created is unknown and stays untracked, therefore kept). N-1 compatible: the previous
release's writer does not know the table and records nothing, so an environment it creates is
untracked and a later deletion keeps it (the safe direction: nothing is deleted that was not
recorded). Roll out the release that writes the rows before any release that deletes them.

The downgrade refuses (``55000``) while any row exists, because dropping the table would turn
tracked environments into untracked, kept ones; the deletion's database step clears a tenant's
rows, and an operator who accepts the loss deletes the rest.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9a4d6c2e8b17"
down_revision: str | Sequence[str] | None = "7b2d4f8a1c36"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "trustbridge_app"
OWNER_ROLE = "trustbridge_owner"

_TABLE = """
CREATE TABLE IF NOT EXISTS github_environments (
    tenant_id uuid NOT NULL,
    host text NOT NULL,
    repository_id bigint NOT NULL,
    environment_name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, repository_id, environment_name)
)
"""

_REFUSE_WITH_ROWS = (
    "DO $$ BEGIN IF to_regclass('public.github_environments') IS NOT NULL AND "
    "EXISTS (SELECT 1 FROM github_environments) THEN "
    "RAISE EXCEPTION 'cannot downgrade: tracked github environments exist' "
    "USING ERRCODE = '55000'; END IF; END $$"
)


def _if_role(role: str, body: str) -> str:
    """A DO block running `body` only if `role` exists."""
    return (
        f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "  # noqa: S608 -- constants only
        f"{body} END IF; END $$"
    )


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute(_TABLE)
    op.execute(
        _if_role(
            APP_ROLE,
            f"GRANT SELECT, INSERT, DELETE, UPDATE (created_at) ON github_environments "
            f"TO {APP_ROLE}; "
            "CREATE POLICY tenant_isolation ON github_environments FOR ALL "
            f"TO {APP_ROLE} USING (tenant_id = (SELECT tb_current_tenant())) "
            "WITH CHECK (tenant_id = (SELECT tb_current_tenant()));",
        )
    )
    op.execute(
        _if_role(
            OWNER_ROLE,
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON github_environments TO {OWNER_ROLE}; "
            f"CREATE POLICY owner_all ON github_environments FOR ALL TO {OWNER_ROLE} "
            "USING (true) WITH CHECK (true);",
        )
    )
    # SET LOCAL lasts to the end of the transaction, which may carry later revisions: end it here.
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")


def downgrade() -> None:
    """Downgrade schema: the table goes, refused while it holds a tracked environment."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("LOCK TABLE github_environments IN ACCESS EXCLUSIVE MODE")
    op.execute(_REFUSE_WITH_ROWS)
    op.execute("-- squawk-ignore ban-drop-table\nDROP TABLE IF EXISTS github_environments")
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
