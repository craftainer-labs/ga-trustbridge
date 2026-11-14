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

* ``tb_delete_tenant(uuid, text, bigint)`` (the ``6f3b9d2e4a85`` body) also lists
  ``github_environments`` in its emptiness check, before and after the delete: the backstop of the
  database step, which clears the table. Same signature, return type, owner and grants;
  ``search_path`` stays ``pg_catalog, public, pg_temp``. The live body is checked first
  (``md5(prosrc)`` of the expected text, as in ``4e9a1c7b5d23``): a drifted function fails the
  migration (``55000``) instead of being overwritten, on upgrade and on downgrade (which restores
the ``6f3b9d2e4a85`` body).

Expand-only (NFR-0027): one new table and one function body that also checks it, no backfill
(what an earlier release created is unknown and stays untracked, therefore kept). N-1
compatible: the previous release's writer does not know the table and records nothing, so an
environment it creates is untracked and a later deletion keeps it (the safe direction: nothing is
deleted that was not recorded). Roll out the release that writes the rows before any release
that deletes them.

The downgrade refuses (``55000``) while any row exists, because dropping the table would turn
tracked environments into untracked, kept ones; the deletion's database step clears a tenant's
rows, and an operator who accepts the loss deletes the rest.
"""

import hashlib
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9a4d6c2e8b17"
REVISION = revision
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

DELETE = "tb_delete_tenant(uuid, text, bigint)"

# The body 6f3b9d2e4a85 left (verbatim), restored by the downgrade and checked before the upgrade.
_PREVIOUS_DELETE_FN = """
CREATE OR REPLACE FUNCTION tb_delete_tenant(p_id uuid, p_holder text, p_fence bigint) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    v_row public.tenants%ROWTYPE;
    v_host text;
    v_inst bigint;
    v_not_before timestamptz;
    v_holder text;
    v_fence bigint;
    v_expires timestamptz;
    v_has_deletion boolean;
    v_left text;
BEGIN
    -- The advisory lock comes first (it is keyed on the installation, read without a lock), then
    -- the deletion row and then the tenant row (the lock order of every function taking more
    -- than one lock): tb_upsert_tenant takes the advisory lock and then touches the tenant
    -- row, so no order can deadlock.
    SELECT t.host, t.installation_id INTO v_host, v_inst FROM public.tenants t WHERE t.id = p_id;
    IF NOT FOUND THEN
        RETURN;  -- already gone: a resumed database step is a no-op
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('tb_tenant:' || v_host || ':' || v_inst::text, 0));
    SELECT d.not_before, d.holder, d.fence, d.expires_at
      INTO v_not_before, v_holder, v_fence, v_expires FROM public.tenant_deletions d
     WHERE d.tenant_id = p_id AND d.state = 'database' FOR UPDATE;
    v_has_deletion := FOUND;
    SELECT * INTO v_row FROM public.tenants WHERE id = p_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN;  -- deleted while this call waited for the lock
    END IF;
    IF v_row.admin_disabled_at IS NULL THEN
        RAISE EXCEPTION 'tenant is not admin-disabled' USING ERRCODE = '55000';
    END IF;
    -- `database` is a non-terminal (running) state; the caller must hold its live lease
    -- (holder, fence and an unexpired `expires_at`, by the wall clock).
    IF NOT v_has_deletion THEN
        RAISE EXCEPTION 'tenant has no deletion in its database step' USING ERRCODE = '55000';
    END IF;
    IF p_holder IS NULL OR p_fence IS NULL OR v_holder IS DISTINCT FROM p_holder
       OR v_fence IS DISTINCT FROM p_fence OR v_expires IS NULL
       OR v_expires <= pg_catalog.clock_timestamp() THEN
        RAISE EXCEPTION 'deletion lease lost' USING ERRCODE = 'TB003';
    END IF;
    IF v_not_before IS NOT NULL AND v_not_before > pg_catalog.clock_timestamp() THEN
        RAISE EXCEPTION 'the deletion grace window has not elapsed' USING ERRCODE = '55000';
    END IF;

SELECT concat_ws(', ',
    CASE WHEN EXISTS (SELECT 1 FROM public.tenant_repositories WHERE tenant_id = p_id)
         THEN 'tenant_repositories' END,
    CASE WHEN EXISTS (SELECT 1 FROM public.scope_leases WHERE tenant_id = p_id)
         THEN 'scope_leases' END,
    CASE WHEN EXISTS (SELECT 1 FROM public.adapter_resources WHERE tenant_id = p_id)
         THEN 'adapter_resources' END,
    CASE WHEN EXISTS (SELECT 1 FROM public.adapter_credentials WHERE tenant_id = p_id)
         THEN 'adapter_credentials' END,
    CASE WHEN EXISTS (SELECT 1 FROM public.credential_operations WHERE tenant_id = p_id)
         THEN 'credential_operations' END,
    CASE WHEN EXISTS (SELECT 1 FROM public.webhook_deliveries
                      WHERE tenant_id = p_id AND status <> 'deleted')
         THEN 'webhook_deliveries' END)
 INTO v_left;
    IF v_left <> '' THEN
        RAISE EXCEPTION 'tenant still has rows in: %', v_left USING ERRCODE = '55000';
    END IF;
    DELETE FROM public.tenants WHERE id = p_id;
    -- The tables have no foreign key, so a writer is not blocked by the row lock: check once more
    -- and roll the delete back if a row committed in between.

SELECT concat_ws(', ',
    CASE WHEN EXISTS (SELECT 1 FROM public.tenant_repositories WHERE tenant_id = p_id)
         THEN 'tenant_repositories' END,
    CASE WHEN EXISTS (SELECT 1 FROM public.scope_leases WHERE tenant_id = p_id)
         THEN 'scope_leases' END,
    CASE WHEN EXISTS (SELECT 1 FROM public.adapter_resources WHERE tenant_id = p_id)
         THEN 'adapter_resources' END,
    CASE WHEN EXISTS (SELECT 1 FROM public.adapter_credentials WHERE tenant_id = p_id)
         THEN 'adapter_credentials' END,
    CASE WHEN EXISTS (SELECT 1 FROM public.credential_operations WHERE tenant_id = p_id)
         THEN 'credential_operations' END,
    CASE WHEN EXISTS (SELECT 1 FROM public.webhook_deliveries
                      WHERE tenant_id = p_id AND status <> 'deleted')
         THEN 'webhook_deliveries' END)
 INTO v_left;
    IF v_left <> '' THEN
        RAISE EXCEPTION 'tenant still has rows in: %', v_left USING ERRCODE = '55000';
    END IF;
END
$$
"""  # noqa: E501 -- the 6f3b9d2e4a85 body, verbatim


def _replaced(text: str, old: str, new: str, count: int) -> str:
    """`text` with `old` replaced by `new`; any other number of matches is an error."""
    if text.count(old) != count:
        raise ValueError(f"expected text not found {count} time(s): {old[:60]!r}")
    return text.replace(old, new)


# The list appears twice: before the delete and in the re-check after it.
_DELETE_FN = _replaced(
    _PREVIOUS_DELETE_FN,
    "         THEN 'webhook_deliveries' END)\n",
    "         THEN 'webhook_deliveries' END,\n"
    "    CASE WHEN EXISTS (SELECT 1 FROM public.github_environments WHERE tenant_id = p_id)\n"
    "         THEN 'github_environments' END)\n",
    2,
)


def _body_md5(function_sql: str) -> str:
    """The md5 of the text between the outer ``$$`` of `function_sql`, which is the `prosrc`."""
    body = function_sql[function_sql.index("$$") + 2 : function_sql.rindex("$$")]
    return hashlib.md5(body.encode(), usedforsecurity=False).hexdigest()


def _assert_body(function_sql: str, source: str) -> str:
    """A DO block failing (55000) unless the live body is exactly `function_sql`'s body."""
    return (
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_proc WHERE "  # noqa: S608 -- constants only
        f"oid = to_regprocedure('{DELETE}') "
        f"AND pg_catalog.md5(prosrc) = '{_body_md5(function_sql)}') THEN "
        f"RAISE EXCEPTION '{DELETE} is not the {source} body' "
        "USING ERRCODE = '55000'; END IF; END $$"
    )


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
    op.execute(_assert_body(_PREVIOUS_DELETE_FN, "6f3b9d2e4a85"))
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
    # Same signature and return type, so the owner and the grants stay.
    op.execute(_DELETE_FN)
    # SET LOCAL lasts to the end of the transaction, which may carry later revisions: end it here.
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")


def downgrade() -> None:
    """Downgrade schema: the table goes, refused while it holds a tracked environment."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("LOCK TABLE github_environments IN ACCESS EXCLUSIVE MODE")
    op.execute(_REFUSE_WITH_ROWS)
    op.execute(_assert_body(_DELETE_FN, REVISION))
    op.execute(_PREVIOUS_DELETE_FN)
    op.execute("-- squawk-ignore ban-drop-table\nDROP TABLE IF EXISTS github_environments")
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
