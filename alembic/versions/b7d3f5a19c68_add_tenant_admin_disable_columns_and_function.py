"""add the tenant admin-disable columns and tb_set_tenant_disabled (NG6e)

Revision ID: b7d3f5a19c68
Revises: a1c4e8d27b53
Create Date: 2026-11-06 12:00:00.000000

ADR 0033 "Tenant disable": a tenant row gets ``admin_disabled_at``, ``admin_disabled_by`` (the
actor's ``sub``) and ``admin_disable_reason``, separate from ``state``. ``tb_upsert_tenant`` and
the installation sync never write them (ADR 0031 invariant 7; the upsert function is not changed).

* Three nullable columns with no default: a catalog-only change, no rewrite. A
  ``CHECK`` keeps the three in step (all null, or the time and the actor both set); it is added
  ``NOT VALID`` and then validated, so the table is not held under a long lock. ``tenants`` is one
  row per installation, so the validation scan is short.
* ``tb_list_tenants()`` and ``tb_get_tenant(uuid)`` return the two new columns. A function's
  return row type cannot change with ``CREATE OR REPLACE`` (SQLSTATE 42P13), so each is dropped
  and created again in this one transaction, and the ``REVOKE`` and ``GRANT EXECUTE`` are applied
  again as in ``d4b8f1e60a27`` (roles are created outside migrations; the grants run only when
  the role exists). ``deleting`` stays the constant ``false`` until NG6h.
* ``tb_set_tenant_disabled(uuid, text, text, boolean)`` is a ``SECURITY DEFINER`` function with a
  fixed ``search_path`` that makes the disable or enable one atomic row-locked write and returns
  the before and after values for the audit event. It keeps the app role's privileges narrow: the
  admin columns change only through it. A repeat disable keeps the original time, actor and
  reason. Enabling has a hook point (``v_deleting``) where NG6h refuses a tenant with a
  non-terminal deletion.

N-1 compatible (NFR-0027): the previous release never reads or writes the new columns, and the
only readers of the two functions select their old columns by name, so the extra columns change
nothing for them. Downgrade restores the old function bodies exactly and drops the new objects.
Lock and statement timeouts are set first. ``schema_compat`` is not touched.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d3f5a19c68"
down_revision: str | Sequence[str] | None = "a1c4e8d27b53"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "trustbridge_app"
OWNER_ROLE = "trustbridge_owner"
READ_FUNCTIONS = ("tb_list_tenants()", "tb_get_tenant(uuid)")
SET_FUNCTION = "tb_set_tenant_disabled(uuid, text, text, boolean)"

_LIST_FN = """
        CREATE FUNCTION tb_list_tenants()
        RETURNS TABLE (
            id uuid, host text, installation_id bigint, org text, org_id bigint, state text,
            admin_disabled_at timestamptz, deleting boolean,
            admin_disabled_by text, admin_disable_reason text
        )
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT t.id, t.host::text, t.installation_id, t.org::text, t.org_id, t.state::text,
                   t.admin_disabled_at, false, t.admin_disabled_by, t.admin_disable_reason
            FROM public.tenants t ORDER BY t.created_at, t.id
        $$
        """

_GET_FN = """
CREATE FUNCTION tb_get_tenant(p_id uuid)
RETURNS TABLE (
    id uuid, host text, installation_id bigint, org text, org_id bigint, state text,
    admin_disabled_at timestamptz, deleting boolean,
    admin_disabled_by text, admin_disable_reason text
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT t.id, t.host::text, t.installation_id, t.org::text, t.org_id, t.state::text,
           t.admin_disabled_at, false, t.admin_disabled_by, t.admin_disable_reason
    FROM public.tenants t WHERE t.id = p_id
$$
"""

_SET_FN = """
CREATE FUNCTION tb_set_tenant_disabled(
    p_id uuid, p_by text, p_reason text, p_disable boolean
) RETURNS TABLE (
    tenant_found boolean,
    before_disabled_at timestamptz, before_disabled_by text, before_disable_reason text,
    after_disabled_at timestamptz, after_disabled_by text, after_disable_reason text
)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_row public.tenants%ROWTYPE;
    v_deleting boolean := false;  -- NG6h: true while a non-terminal tenant_deletions row exists
BEGIN
    IF p_disable IS NULL THEN
        RAISE EXCEPTION 'invalid disable request' USING ERRCODE = '22023';
    END IF;
    IF p_disable THEN
        IF p_by IS NULL OR length(p_by) NOT BETWEEN 1 AND 255
           OR p_reason IS NULL OR length(p_reason) NOT BETWEEN 1 AND 500
           OR p_by ~ '[\\x01-\\x1f\\x7f-\\x9f]'
           OR p_reason ~ '[\\x01-\\x1f\\x7f-\\x9f]' THEN
            RAISE EXCEPTION 'invalid disable request' USING ERRCODE = '22023';
        END IF;
    END IF;
    SELECT * INTO v_row FROM public.tenants WHERE id = p_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN QUERY SELECT false, NULL::timestamptz, NULL::text, NULL::text,
                            NULL::timestamptz, NULL::text, NULL::text;
        RETURN;
    END IF;
    before_disabled_at := v_row.admin_disabled_at;
    before_disabled_by := v_row.admin_disabled_by;
    before_disable_reason := v_row.admin_disable_reason;
    IF p_disable THEN
        IF v_row.admin_disabled_at IS NULL THEN
            UPDATE public.tenants
               SET admin_disabled_at = now(), admin_disabled_by = p_by,
                   admin_disable_reason = p_reason
             WHERE id = p_id
         RETURNING admin_disabled_at, admin_disabled_by, admin_disable_reason
              INTO after_disabled_at, after_disabled_by, after_disable_reason;
        ELSE
            after_disabled_at := v_row.admin_disabled_at;
            after_disabled_by := v_row.admin_disabled_by;
            after_disable_reason := v_row.admin_disable_reason;
        END IF;
    ELSE
        IF v_deleting THEN
            RAISE EXCEPTION 'tenant has a deletion in progress' USING ERRCODE = '55000';
        END IF;
        IF v_row.admin_disabled_at IS NOT NULL THEN
            UPDATE public.tenants
               SET admin_disabled_at = NULL, admin_disabled_by = NULL,
                   admin_disable_reason = NULL
             WHERE id = p_id;
        END IF;
    END IF;
    tenant_found := true;
    RETURN NEXT;
END
$$
"""

# The bodies of the previous release, byte for byte (d4b8f1e60a27 and a1c4e8d27b53), restored
# by the downgrade.
_OLD_LIST_FN = """
        CREATE OR REPLACE FUNCTION tb_list_tenants()
        RETURNS TABLE (
            id uuid, host text, installation_id bigint, org text, org_id bigint, state text,
            admin_disabled_at timestamptz, deleting boolean
        )
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT t.id, t.host::text, t.installation_id, t.org::text, t.org_id, t.state::text,
                   NULL::timestamptz, false
            FROM public.tenants t ORDER BY t.created_at, t.id
        $$
        """

_OLD_GET_FN = """
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


def _grant(signatures: Sequence[str]) -> None:
    for signature in signatures:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    grants = ", ".join(signatures)
    op.execute(_if_role(APP_ROLE, f"GRANT EXECUTE ON FUNCTION {grants} TO {APP_ROLE};"))
    op.execute(_if_role(OWNER_ROLE, f"GRANT EXECUTE ON FUNCTION {grants} TO {OWNER_ROLE};"))


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute(
        "ALTER TABLE tenants ADD COLUMN admin_disabled_at timestamptz, "
        "ADD COLUMN admin_disabled_by text, ADD COLUMN admin_disable_reason text"
    )
    op.execute(
        "ALTER TABLE tenants ADD CONSTRAINT ck_tenants_admin_disabled CHECK ("
        "(admin_disabled_at IS NULL AND admin_disabled_by IS NULL "
        "AND admin_disable_reason IS NULL) OR "
        "(admin_disabled_at IS NOT NULL AND admin_disabled_by IS NOT NULL "
        "AND admin_disable_reason IS NOT NULL)) NOT VALID"
    )
    # `tenants` has one row per installation and the three columns are all null, so validating
    # in the same transaction holds the lock for a moment only.
    op.execute(
        "-- squawk-ignore constraint-missing-not-valid\n"
        "ALTER TABLE tenants VALIDATE CONSTRAINT ck_tenants_admin_disabled"
    )
    # Dropped and created again in this transaction (a row type cannot change in place); the
    # only readers select named columns, so the previous release is unaffected.
    op.execute("-- squawk-ignore ban-drop-function\nDROP FUNCTION tb_list_tenants()")
    op.execute("-- squawk-ignore ban-drop-function\nDROP FUNCTION tb_get_tenant(uuid)")
    op.execute(_LIST_FN)
    op.execute(_GET_FN)
    op.execute(_SET_FN)
    _grant((*READ_FUNCTIONS, SET_FUNCTION))


def downgrade() -> None:
    """Downgrade schema; refused while any tenant is admin-disabled."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    # The lock keeps a concurrent disable out between the check and the column drop.
    op.execute("LOCK TABLE tenants IN SHARE ROW EXCLUSIVE MODE")
    # The previous release has no admin-disable column, so dropping them would silently enable
    # the tenant again. Enable them (or accept it by clearing the columns) before going back.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM tenants WHERE admin_disabled_at IS NOT NULL) THEN "
        "RAISE EXCEPTION 'cannot downgrade: tenants are admin-disabled; enable them first' "
        "USING ERRCODE = '55000'; END IF; END $$"
    )
    op.execute(f"DROP FUNCTION IF EXISTS {SET_FUNCTION}")
    op.execute("DROP FUNCTION IF EXISTS tb_get_tenant(uuid)")
    op.execute("DROP FUNCTION IF EXISTS tb_list_tenants()")
    op.execute(_OLD_LIST_FN)
    op.execute(_OLD_GET_FN)
    _grant(READ_FUNCTIONS)
    op.execute("ALTER TABLE tenants DROP CONSTRAINT IF EXISTS ck_tenants_admin_disabled")
    op.execute(
        "ALTER TABLE tenants DROP COLUMN IF EXISTS admin_disable_reason, "
        "DROP COLUMN IF EXISTS admin_disabled_by, DROP COLUMN IF EXISTS admin_disabled_at"
    )
