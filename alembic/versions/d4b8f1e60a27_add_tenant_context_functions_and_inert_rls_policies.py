"""add the tenant-context functions and inert row-level-security policies (NG6a, release N)

Revision ID: d4b8f1e60a27
Revises: a7e2c94b1d63
Create Date: 2026-11-03 12:00:00.000000

Release N ("expand") of ADR 0031's two-release rollout. It creates, while row-level security is
still **disabled** on every table, so the policies exist and filter nothing:

* ``tb_current_tenant()``: ``NULLIF(current_setting('app.tenant_id', true), '')::uuid``;
* three ``SECURITY DEFINER`` functions with a fixed ``search_path = pg_catalog, public`` that are
  the only unscoped paths to ``tenants``: ``tb_resolve_tenant``, ``tb_upsert_tenant`` and
  ``tb_list_tenants``;
* the ``tenant_isolation`` and ``owner_all`` policies on the six tenant-owned tables
  (``tenants`` is keyed on ``id``, for SELECT and UPDATE only, so the app role can neither insert
  nor delete a tenant once RLS is on).

``tb_delete_tenant`` belongs to NG6h (tenant data deletion): it needs the ``tenant_deletions``
table, which does not exist yet. ``tb_list_tenants`` is created here with its final return type
and constant placeholders for the two columns later items add (``admin_disabled_at`` is NULL and
``deleting`` is false); NG6e and NG6h replace it with ``CREATE OR REPLACE FUNCTION`` (same
signature) once the ``tenants.admin_disabled_*`` columns and ``tenant_deletions`` exist.

It does not ``ENABLE`` or ``FORCE`` row-level security and does not bump ``schema_compat``
(``GREATEST`` with 1, so no change): release N+1 does both. N-1 code never sets the tenant
setting and, with RLS off, does not need to.

Roles are created outside migrations (the DBA or the Helm chart); this revision only ``GRANT``s.
With ``MODE=production`` it refuses to run when ``trustbridge_app`` or ``trustbridge_owner`` is
absent, when ``trustbridge_app`` is a superuser or has ``BYPASSRLS``, or when it owns a
tenant-owned table. Elsewhere (dev, compose, tests) a missing role is skipped, as in the earlier
revisions: grants and the policies naming that role are not created.

Lock-safe: only catalog changes (functions, policies) and no table rewrite or index build; the
migration sets lock and statement timeouts first.
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4b8f1e60a27"
down_revision: str | Sequence[str] | None = "a7e2c94b1d63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_COMPAT_VERSION = 1
APP_ROLE = "trustbridge_app"
OWNER_ROLE = "trustbridge_owner"
# The tenant-owned tables other than `tenants` (each has a `tenant_id` column).
KEYED_TABLES = (
    "tenant_repositories",
    "scope_leases",
    "adapter_resources",
    "adapter_credentials",
    "credential_operations",
)
# The tables whose ownership by the app role would silently defeat RLS (`audit_events` is NG6c's
# and is simply absent until then).
PROTECTED_TABLES = ("tenants", *KEYED_TABLES, "audit_events")
FUNCTION_SIGNATURES = (
    "tb_current_tenant()",
    "tb_resolve_tenant(text, bigint)",
    "tb_upsert_tenant(text, bigint, text, bigint, text)",
    "tb_list_tenants()",
)


def is_production() -> bool:
    """True when the process runs with ``MODE=production`` (the application's own setting)."""
    return os.environ.get("MODE") == "production"


def check_roles(
    bind: Connection,
    *,
    production: bool,
    app_role: str = APP_ROLE,
    owner_role: str = OWNER_ROLE,
) -> None:
    """Refuse (in production) a missing role, a bypassing app role or one that owns a table."""
    if not production:
        return
    roles = {
        row.rolname: row
        for row in bind.execute(
            sa.text(
                "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles "
                "WHERE rolname = ANY(CAST(:names AS text[]))"
            ),
            {"names": [app_role, owner_role]},
        )
    }
    for name in (app_role, owner_role):
        if name not in roles:
            raise RuntimeError(f"database role {name!r} does not exist; create it before migrating")
    app = roles[app_role]
    if app.rolsuper or app.rolbypassrls:
        raise RuntimeError(f"database role {app_role!r} is a superuser or has BYPASSRLS")
    owned = (
        bind.execute(
            sa.text(
                "SELECT c.relname FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner "
                "WHERE r.rolname = :app AND c.relkind IN ('r', 'p') "
                "AND c.relnamespace = 'public'::regnamespace "
                "AND c.relname = ANY(CAST(:tables AS text[])) ORDER BY c.relname"
            ),
            {"app": app_role, "tables": list(PROTECTED_TABLES)},
        )
        .scalars()
        .all()
    )
    if owned:
        raise RuntimeError(f"database role {app_role!r} owns tenant tables: {', '.join(owned)}")


def _if_role(role: str, body: str) -> str:
    """A DO block running `body` only if `role` exists."""
    return (
        f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "  # noqa: S608 -- constants only
        f"{body} END IF; END $$"
    )


def _policies_sql() -> list[str]:
    """The `tenant_isolation` and `owner_all` policies, each only if its role exists."""
    app: list[str] = []
    owner: list[str] = []
    for table in KEYED_TABLES:
        app.append(
            f"CREATE POLICY tenant_isolation ON {table} FOR ALL TO {APP_ROLE} "
            "USING (tenant_id = (SELECT tb_current_tenant())) "
            "WITH CHECK (tenant_id = (SELECT tb_current_tenant()));"
        )
    app.append(
        f"CREATE POLICY tenant_isolation_select ON tenants FOR SELECT TO {APP_ROLE} "
        "USING (id = (SELECT tb_current_tenant()));"
    )
    app.append(
        f"CREATE POLICY tenant_isolation_update ON tenants FOR UPDATE TO {APP_ROLE} "
        "USING (id = (SELECT tb_current_tenant())) "
        "WITH CHECK (id = (SELECT tb_current_tenant()));"
    )
    for table in ("tenants", *KEYED_TABLES):
        owner.append(
            f"CREATE POLICY owner_all ON {table} FOR ALL TO {OWNER_ROLE} "
            "USING (true) WITH CHECK (true);"
        )
    return [_if_role(APP_ROLE, " ".join(app)), _if_role(OWNER_ROLE, " ".join(owner))]


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    if not op.get_context().as_sql:  # offline rendering (the SQL lint) cannot query the catalog
        check_roles(op.get_bind(), production=is_production())

    op.execute(
        """
        CREATE OR REPLACE FUNCTION tb_current_tenant() RETURNS uuid
        LANGUAGE sql STABLE
        AS $$ SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION tb_resolve_tenant(p_host text, p_installation_id bigint)
        RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT t.id FROM public.tenants t
            WHERE t.host = p_host AND t.installation_id = p_installation_id
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION tb_upsert_tenant(
            p_host text, p_installation_id bigint, p_org text, p_org_id bigint, p_state text
        ) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_id uuid;
        BEGIN
            IF p_state NOT IN ('active', 'suspended', 'deleted') THEN
                RAISE EXCEPTION 'invalid tenant state' USING ERRCODE = '22023';
            END IF;
            INSERT INTO public.tenants (host, installation_id, org, org_id, state)
            VALUES (p_host, p_installation_id, p_org, p_org_id, p_state)
            ON CONFLICT ON CONSTRAINT uq_tenants_host_inst DO UPDATE
                SET org = EXCLUDED.org, org_id = EXCLUDED.org_id, state = EXCLUDED.state
            RETURNING id INTO v_id;
            RETURN v_id;
        END
        $$
        """
    )
    # NG6e and NG6h replace this with CREATE OR REPLACE FUNCTION (same signature) once
    # `tenants.admin_disabled_*` and `tenant_deletions` exist; until then the two columns are
    # constants so callers (`iterate_tenants`) can already rely on the final return type.
    op.execute(
        """
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
    )
    for signature in FUNCTION_SIGNATURES:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    grants = ", ".join(FUNCTION_SIGNATURES)
    op.execute(_if_role(APP_ROLE, f"GRANT EXECUTE ON FUNCTION {grants} TO {APP_ROLE};"))
    op.execute(_if_role(OWNER_ROLE, f"GRANT EXECUTE ON FUNCTION {grants} TO {OWNER_ROLE};"))

    # Created inert: no ENABLE/FORCE ROW LEVEL SECURITY here (release N+1).
    for statement in _policies_sql():
        op.execute(statement)

    op.execute(
        "INSERT INTO schema_compat (id, version) VALUES (1, "  # noqa: S608 -- constants and ints only
        f"{SCHEMA_COMPAT_VERSION}) ON CONFLICT (id) DO UPDATE SET version = "
        f"GREATEST(schema_compat.version, {SCHEMA_COMPAT_VERSION})"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    for table in KEYED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"DROP POLICY IF EXISTS owner_all ON {table}")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_select ON tenants")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_update ON tenants")
    op.execute("DROP POLICY IF EXISTS owner_all ON tenants")
    for signature in reversed(FUNCTION_SIGNATURES):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
