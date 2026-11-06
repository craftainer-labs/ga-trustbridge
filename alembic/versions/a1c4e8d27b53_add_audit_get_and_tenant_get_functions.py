"""add tb_audit_get and tb_get_tenant, the single-row read functions (NG6d-2)

Revision ID: a1c4e8d27b53
Revises: f6d2b8c14a39
Create Date: 2026-11-05 10:00:00.000000

Two new read-only ``SECURITY DEFINER`` functions for the admin API (ADR 0033), both created the
way the earlier definer functions are (a fixed ``search_path``, ``EXECUTE`` revoked from
``PUBLIC`` and granted to ``trustbridge_app`` and ``trustbridge_owner`` only when the role
exists; roles are created outside migrations):

* ``tb_audit_get(uuid)`` returns the one ``audit_events`` row with that id, whatever its tenant.
  It is the single-row twin of ``tb_audit_list`` (``GET /audit-events/{id}`` is instance-scoped,
  and the row-level-security policies hide null-tenant rows from a plain ``SELECT``). The caller
  checks the instance-scoped ``auditor`` role; this function does not.
* ``tb_get_tenant(uuid)`` returns ``tb_list_tenants()``'s columns for one tenant, so a lookup by id
  reads one row by primary key instead of the whole table. It has the same return type, and the
  same constants for ``admin_disabled_at`` and ``deleting`` until NG6e and NG6h replace both
  functions.

``tb_list_tenants()`` is not changed: the ``tenants`` table has no ``admin_disabled_by`` or
``admin_disable_reason`` column yet (NG6e adds them with ``admin_disabled_at``), so there is
nothing to return for them.

N-1 compatible (NFR-0027): only new functions, which the previous release neither calls nor
needs; no table, column, index or grant changes, so there is no expand/contract split and no lock
beyond the catalog. Function ownership is not changed (the owner is whoever runs the migration,
as in ``e5c9a37d2b18``). Lock and statement timeouts are set first. ``schema_compat`` is not
touched.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c4e8d27b53"
down_revision: str | Sequence[str] | None = "f6d2b8c14a39"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "trustbridge_app"
OWNER_ROLE = "trustbridge_owner"
FUNCTIONS = ("tb_audit_get(uuid)", "tb_get_tenant(uuid)")

_AUDIT_GET_FN = """
CREATE OR REPLACE FUNCTION tb_audit_get(p_id uuid) RETURNS SETOF public.audit_events
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT a.* FROM public.audit_events a WHERE a.id = p_id
$$
"""

_TENANT_GET_FN = """
CREATE OR REPLACE FUNCTION tb_get_tenant(p_id uuid)
RETURNS TABLE (
    id uuid, host text, installation_id bigint, org text, org_id bigint, state text,
    admin_disabled_at timestamptz, deleting boolean
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT t.id, t.host::text, t.installation_id, t.org::text, t.org_id, t.state::text,
           NULL::timestamptz, false
    FROM public.tenants t WHERE t.id = p_id
$$
"""


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
    op.execute(_AUDIT_GET_FN)
    op.execute(_TENANT_GET_FN)
    for signature in FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    grants = ", ".join(FUNCTIONS)
    op.execute(_if_role(APP_ROLE, f"GRANT EXECUTE ON FUNCTION {grants} TO {APP_ROLE};"))
    op.execute(_if_role(OWNER_ROLE, f"GRANT EXECUTE ON FUNCTION {grants} TO {OWNER_ROLE};"))


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    for signature in reversed(FUNCTIONS):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
