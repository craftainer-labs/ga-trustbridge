"""add tenant_deletions and the tenant-deletion database functions (NG6h-1, ADR 0034)

Revision ID: 9c2e5a7f1d84
Revises: b7d3f5a19c68
Create Date: 2026-11-13 12:00:00.000000

The database foundation of tenant data deletion (ADR 0034). Nothing in the application calls any of
it yet, so the previous release (N-1) is unaffected (NFR-0027): every change is an addition, or a
function body replaced under the same signature and return type.

* ``tenant_deletions``: the write-ahead record of a deletion. Host-scoped, outside RLS, no foreign
  key to ``tenants`` (it outlives the tenant row). States ``requested``, ``draining``, ``platform``,
  ``github``, ``database``, ``done``, ``failed``, ``cancelled``; the last three are terminal. A
  partial unique index allows one non-terminal row per tenant. The app role gets ``SELECT`` only:
  every write goes through a ``SECURITY DEFINER`` function (``tb_request_tenant_deletion`` here,
  the engine's transitions in NG6h-4a), which runs as the owner role, so the record cannot be
  inserted, rewritten or deleted by the application directly (it is kept as a compliance record).
  ``delete_platform_resources`` stores the request flag (ADR 0034 user decision 1). The record
  holds ids, counts and what was skipped, never the organisation login.
* ``webhook_deliveries.tenant_id`` (nullable uuid, no default: catalog only) with a partial index
  built ``CONCURRENTLY``, and ``ck_wd_status`` widened to allow ``deleted``. The new CHECK is added
  ``NOT VALID``, validated outside the migration transaction (a lock that does not block writes),
  and then swaps names with the old one. The previous release never writes ``tenant_id`` or
  ``deleted``.
* ``tb_list_tenants()`` and ``tb_get_tenant(uuid)``: ``deleting`` is true while a non-terminal
  ``tenant_deletions`` row exists. The row type is already final (``b7d3f5a19c68``), so
  ``CREATE OR REPLACE`` works and the readers, which select named columns, are unaffected.
* ``tb_set_tenant_disabled``: ``enable`` is refused (SQLSTATE 55000) while a non-terminal deletion
  exists. The deletion state is read after the tenant row is locked, so it serialises with
  ``tb_request_tenant_deletion``.
* ``tb_upsert_tenant``: a tenant whose ``state`` is ``deleted`` is returned unchanged (the existing
  id; ``org``, ``org_id`` and ``state`` are kept) instead of being revived by a late event.
  A ``done`` ``tenant_deletions`` row for ``(host, installation_id)`` (or one in its
  ``database`` step whose tenant row is already gone) is a tombstone: the function then returns
  ``NULL`` and creates nothing (an open, ``failed`` or ``cancelled`` deletion is not a tombstone,
  and another installation id of the host is unaffected). The lookup uses a partial index on
  ``state = 'done'``. The function and ``tb_delete_tenant`` first take a transaction-level
  advisory lock on the installation (see ``_lock``; ``tb_request_tenant_deletion`` takes it too),
  so a concurrent delete and upsert are serialised and a deleted tenant cannot be recreated by
  an upsert that checked just before the delete committed.
* ``tb_request_tenant_deletion(...)``: a ``SECURITY DEFINER`` function that, under the tenant row
  lock, returns the open deletion, or checks that the tenant is admin-disabled and that the typed
  confirmation (host, installation id, organisation login) matches, and writes the record. The
  confirmation is compared (case-insensitively, as GitHub logins are) and never stored. The
  optional ``p_not_before`` (default ``NULL``, no delay) is stored in the nullable
  ``not_before`` column as a grace window. The confirmation is validated before the open deletion
  is returned, so a replay needs the right echo too.
* ``tb_delete_tenant(uuid)``: removes the ``tenants`` row as the last step of a deletion. Under the
  row lock it refuses unless the tenant is admin-disabled, has a deletion in its ``database`` step
  (non-terminal, so running) whose ``not_before`` has passed, and has no rows left in the five
  other tenant-owned tables; it checks again after the delete, so a row that committed in
  between rolls the delete back. The tables have no foreign key, so a
  writer is not blocked by the lock: the engine must stop writing for a deleting tenant first.
  It does not touch ``audit_events``, ``tenant_deletions`` or ``webhook_deliveries``.

Functions are ``SECURITY DEFINER`` with a fixed ``search_path``; ``EXECUTE`` is revoked from
``PUBLIC`` and granted to the roles that exist (roles are created outside migrations). The
downgrade is refused while any ``tenant_deletions`` row exists (the compliance record would be
lost) and otherwise restores the previous function bodies. ``schema_compat`` is not touched.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9c2e5a7f1d84"
down_revision: str | Sequence[str] | None = "b7d3f5a19c68"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "trustbridge_app"
OWNER_ROLE = "trustbridge_owner"
NEW_FUNCTIONS = (
    "tb_request_tenant_deletion(uuid, text, bigint, text, text, text, boolean, timestamptz)",
    "tb_delete_tenant(uuid)",
)
CHANGED_FUNCTIONS = (
    "tb_list_tenants()",
    "tb_get_tenant(uuid)",
    "tb_set_tenant_disabled(uuid, text, text, boolean)",
    "tb_upsert_tenant(text, bigint, text, bigint, text)",
)
OPEN = "NOT IN ('done', 'failed', 'cancelled')"


def _lock(host: str, installation_id: str) -> str:
    """PL/pgSQL taking the per-installation advisory lock.

    The key is ``hashtextextended('tb_tenant:' || host || ':' || installation_id, 0)``, a stable
    64-bit hash of the installation's identity (the host name may contain ``:``, but the last
    segment is always the numeric id, so two installations never share the string). The
    application's ``SqlTenants.upsert_installation`` builds the same string. The lock is held to
    the end of the transaction.
    """
    return (
        "PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended("
        f"'tb_tenant:' || {host} || ':' || {installation_id}::text, 0));"
    )


_TABLE = """
CREATE TABLE tenant_deletions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    host text NOT NULL,
    installation_id bigint NOT NULL,
    requested_by_sub text NOT NULL,
    requested_by_iss text NOT NULL,
    requested_at timestamptz NOT NULL DEFAULT now(),
    delete_platform_resources boolean NOT NULL DEFAULT false,
    state text NOT NULL DEFAULT 'requested',
    step_cursor jsonb NOT NULL DEFAULT '{}',
    counts jsonb NOT NULL DEFAULT '{}',
    skipped jsonb NOT NULL DEFAULT '[]',
    holder text,
    fence bigint NOT NULL DEFAULT 0,
    expires_at timestamptz,
    not_before timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    last_error text,
    CONSTRAINT ck_tenant_deletions_state CHECK (state IN (
        'requested', 'draining', 'platform', 'github', 'database', 'done', 'failed', 'cancelled'
    ))
)
"""

_ROW = (
    "t.id, t.host::text, t.installation_id, t.org::text, t.org_id, t.state::text, "  # noqa: S608 -- constants only
    "t.admin_disabled_at, EXISTS (SELECT 1 FROM public.tenant_deletions d "
    f"WHERE d.tenant_id = t.id AND d.state {OPEN}), "
    "t.admin_disabled_by, t.admin_disable_reason"
)
_COLUMNS = """
    id uuid, host text, installation_id bigint, org text, org_id bigint, state text,
    admin_disabled_at timestamptz, deleting boolean,
    admin_disabled_by text, admin_disable_reason text
"""
_PREFIX = "LANGUAGE sql STABLE SECURITY DEFINER\nSET search_path = pg_catalog, public"

_LIST_FN = f"""
CREATE OR REPLACE FUNCTION tb_list_tenants()
RETURNS TABLE ({_COLUMNS})
{_PREFIX}
AS $$
    SELECT {_ROW}
    FROM public.tenants t ORDER BY t.created_at, t.id
$$
"""  # noqa: S608 -- constants only

_GET_FN = f"""
CREATE OR REPLACE FUNCTION tb_get_tenant(p_id uuid)
RETURNS TABLE ({_COLUMNS})
{_PREFIX}
AS $$
    SELECT {_ROW}
    FROM public.tenants t WHERE t.id = p_id
$$
"""  # noqa: S608 -- constants only

_SET_FN = f"""
CREATE OR REPLACE FUNCTION tb_set_tenant_disabled(
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
    v_deleting boolean;
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
    -- Read after the row lock: tb_request_tenant_deletion takes the same lock, so an enable and
    -- a deletion request are serialised and exactly one of them wins.
    SELECT EXISTS (
        SELECT 1 FROM public.tenant_deletions d WHERE d.tenant_id = p_id AND d.state {OPEN}
    ) INTO v_deleting;
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
"""  # noqa: S608 -- constants only

_UPSERT_FN = f"""
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
    -- Serialise with tb_delete_tenant before any check (see the module docstring).
    {_lock("p_host", "p_installation_id")}
    -- Tombstone: once a deletion of this installation is `done` (or is in its `database` step and
    -- tb_delete_tenant has already removed the row), no tenant is created or revived for it.
    -- NULL is returned (a distinct outcome: the other paths always return an id).
    IF EXISTS (
        SELECT 1 FROM public.tenant_deletions d
         WHERE d.host = p_host AND d.installation_id = p_installation_id
           AND (d.state = 'done'
                OR (d.state = 'database' AND NOT EXISTS (
                    SELECT 1 FROM public.tenants t
                     WHERE t.host = p_host AND t.installation_id = p_installation_id)))
    ) THEN
        RETURN NULL;
    END IF;
    -- A tenant whose installation is deleted stays as it is: a late or replayed event cannot
    -- revive it. It is returned unchanged.
    INSERT INTO public.tenants (host, installation_id, org, org_id, state)
    VALUES (p_host, p_installation_id, p_org, p_org_id, p_state)
    ON CONFLICT ON CONSTRAINT uq_tenants_host_inst DO UPDATE
        SET org = CASE WHEN tenants.state = 'deleted' THEN tenants.org ELSE EXCLUDED.org END,
            org_id = CASE WHEN tenants.state = 'deleted'
                          THEN tenants.org_id ELSE EXCLUDED.org_id END,
            state = CASE WHEN tenants.state = 'deleted' THEN tenants.state ELSE EXCLUDED.state END
    RETURNING id INTO v_id;
    RETURN v_id;
END
$$
"""  # noqa: S608 -- constants only

_REQUEST_FN = f"""
CREATE FUNCTION tb_request_tenant_deletion(
    p_tenant uuid, p_host text, p_installation_id bigint, p_org text,
    p_by_sub text, p_by_iss text, p_delete_platform boolean,
    p_not_before timestamptz DEFAULT NULL
) RETURNS TABLE (
    outcome text, deletion_id uuid, state text, requested_at timestamptz,
    delete_platform_resources boolean
)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_row public.tenants%ROWTYPE;
    v_open public.tenant_deletions%ROWTYPE;
    v_new public.tenant_deletions%ROWTYPE;
    v_host text;
    v_inst bigint;
BEGIN
    IF p_by_sub IS NULL OR length(p_by_sub) NOT BETWEEN 1 AND 255
       OR p_by_iss IS NULL OR length(p_by_iss) NOT BETWEEN 1 AND 2048
       OR p_delete_platform IS NULL THEN
        RAISE EXCEPTION 'invalid deletion request' USING ERRCODE = '22023';
    END IF;
    -- Advisory lock first (as tb_upsert_tenant and tb_delete_tenant take it), so an upsert that
    -- read "not deleting" cannot interleave with this request; then the row lock.
    SELECT t.host, t.installation_id INTO v_host, v_inst FROM public.tenants t
     WHERE t.id = p_tenant;
    IF FOUND THEN
        {_lock("v_host", "v_inst")}
    END IF;
    SELECT * INTO v_row FROM public.tenants WHERE id = p_tenant FOR UPDATE;
    IF NOT FOUND THEN
        RETURN QUERY SELECT 'not_found'::text, NULL::uuid, NULL::text, NULL::timestamptz,
                            NULL::boolean;
        RETURN;
    END IF;
    IF v_row.admin_disabled_at IS NULL THEN
        RETURN QUERY SELECT 'not_disabled'::text, NULL::uuid, NULL::text, NULL::timestamptz,
                            NULL::boolean;
        RETURN;
    END IF;
    IF v_row.host IS DISTINCT FROM p_host
       OR v_row.installation_id IS DISTINCT FROM p_installation_id
       OR lower(v_row.org) IS DISTINCT FROM lower(p_org) THEN
        RETURN QUERY SELECT 'confirmation_mismatch'::text, NULL::uuid, NULL::text,
                            NULL::timestamptz, NULL::boolean;
        RETURN;
    END IF;
    SELECT * INTO v_open FROM public.tenant_deletions d
     WHERE d.tenant_id = p_tenant AND d.state {OPEN};
    IF FOUND THEN
        RETURN QUERY SELECT 'existing'::text, v_open.id, v_open.state, v_open.requested_at,
                            v_open.delete_platform_resources;
        RETURN;
    END IF;
    INSERT INTO public.tenant_deletions (
        tenant_id, host, installation_id, requested_by_sub, requested_by_iss,
        delete_platform_resources, not_before
    ) VALUES (
        p_tenant, v_row.host, v_row.installation_id, p_by_sub, p_by_iss, p_delete_platform,
        p_not_before
    ) RETURNING * INTO v_new;
    RETURN QUERY SELECT 'created'::text, v_new.id, v_new.state, v_new.requested_at,
                        v_new.delete_platform_resources;
END
$$
"""  # noqa: S608 -- constants only

_REMAINING = """
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
         THEN 'credential_operations' END)
"""

_DELETE_FN = f"""
CREATE FUNCTION tb_delete_tenant(p_id uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_row public.tenants%ROWTYPE;
    v_host text;
    v_inst bigint;
    v_not_before timestamptz;
    v_left text;
BEGIN
    -- The advisory lock comes first (it is keyed on the installation, read without a lock), then
    -- the row lock: tb_upsert_tenant takes the advisory lock and then touches the row, so no
    -- order can deadlock.
    SELECT t.host, t.installation_id INTO v_host, v_inst FROM public.tenants t WHERE t.id = p_id;
    IF NOT FOUND THEN
        RETURN;  -- already gone: a resumed database step is a no-op
    END IF;
    {_lock("v_host", "v_inst")}
    SELECT * INTO v_row FROM public.tenants WHERE id = p_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN;  -- deleted while this call waited for the lock
    END IF;
    IF v_row.admin_disabled_at IS NULL THEN
        RAISE EXCEPTION 'tenant is not admin-disabled' USING ERRCODE = '55000';
    END IF;
    -- `database` is a non-terminal (running) state. NG6h-4a adds the holder and fence arguments
    -- and checks them here (the columns exist, but nothing passes them yet).
    SELECT d.not_before INTO v_not_before FROM public.tenant_deletions d
     WHERE d.tenant_id = p_id AND d.state = 'database';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'tenant has no deletion in its database step' USING ERRCODE = '55000';
    END IF;
    IF v_not_before IS NOT NULL AND v_not_before > now() THEN
        RAISE EXCEPTION 'the deletion grace window has not elapsed' USING ERRCODE = '55000';
    END IF;
    {_REMAINING} INTO v_left;
    IF v_left <> '' THEN
        RAISE EXCEPTION 'tenant still has rows in: %', v_left USING ERRCODE = '55000';
    END IF;
    DELETE FROM public.tenants WHERE id = p_id;
    -- The tables have no foreign key, so a writer is not blocked by the row lock: check once more
    -- and roll the delete back if a row committed in between.
    {_REMAINING} INTO v_left;
    IF v_left <> '' THEN
        RAISE EXCEPTION 'tenant still has rows in: %', v_left USING ERRCODE = '55000';
    END IF;
END
$$
"""  # noqa: S608 -- constants only

# The bodies of the previous release (b7d3f5a19c68 and d4b8f1e60a27), restored by the downgrade.
_OLD_ROW = (
    "t.id, t.host::text, t.installation_id, t.org::text, t.org_id, t.state::text, "
    "t.admin_disabled_at, false, t.admin_disabled_by, t.admin_disable_reason"
)


def _replaced(body: str, old: str, new: str) -> str:
    """`body` with `old` replaced by `new`; a no-match is an error, never a silent no-op."""
    if old not in body:
        raise ValueError(f"expected text not found in the function body: {old[:60]!r}")
    return body.replace(old, new)


_OLD_LIST_FN = _replaced(_LIST_FN, _ROW, _OLD_ROW)
_OLD_GET_FN = _replaced(_GET_FN, _ROW, _OLD_ROW)
_OLD_SET_FN = _replaced(
    _replaced(
        _SET_FN,
        f"""    SELECT EXISTS (
        SELECT 1 FROM public.tenant_deletions d WHERE d.tenant_id = p_id AND d.state {OPEN}
    ) INTO v_deleting;
""",  # noqa: S608 -- constants only
        "",
    ),
    "    v_deleting boolean;\n",
    "    v_deleting boolean := false;  -- NG6h: a non-terminal deletion exists\n",
)
_OLD_UPSERT_FN = """
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

_NEW_STATUS = "status IN ('pending', 'processed', 'dead', 'deleted')"
_OLD_STATUS = "status IN ('pending', 'processed', 'dead')"


def _if_role(role: str, body: str) -> str:
    """A DO block running `body` only if `role` exists."""
    return (
        f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "  # noqa: S608 -- constants only
        f"{body} END IF; END $$"
    )


def _grant_functions(signatures: Sequence[str]) -> None:
    for signature in signatures:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    grants = ", ".join(signatures)
    op.execute(_if_role(APP_ROLE, f"GRANT EXECUTE ON FUNCTION {grants} TO {APP_ROLE};"))
    op.execute(_if_role(OWNER_ROLE, f"GRANT EXECUTE ON FUNCTION {grants} TO {OWNER_ROLE};"))


def _has_constraint(name: str) -> str:
    return (
        "EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'public.webhook_deliveries'::regclass"  # noqa: S608 -- constants only
        f" AND conname = '{name}')"
    )


def _swap_status_check(*, add: str, check: str, old: str) -> None:
    """Replace ``ck_wd_status`` by ``check`` without a long lock (retry-safe).

    The new constraint is added ``NOT VALID`` (brief lock), validated in autocommit (a lock that
    does not block writes), and only then replaces the old one under the same name.
    """
    op.execute(
        f"DO $$ BEGIN IF NOT {_has_constraint(add)} THEN "
        f"ALTER TABLE webhook_deliveries ADD CONSTRAINT {add} CHECK ({check}) NOT VALID; "
        "END IF; END $$"
    )
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = '600s'")
        op.execute(
            "-- squawk-ignore constraint-missing-not-valid, prefer-robust-stmts\n"
            f"ALTER TABLE webhook_deliveries VALIDATE CONSTRAINT {add}"
        )
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        f"DO $$ BEGIN IF {_has_constraint(old)} THEN "
        f"ALTER TABLE webhook_deliveries DROP CONSTRAINT {old}; END IF; "
        f"ALTER TABLE webhook_deliveries RENAME CONSTRAINT {add} TO ck_wd_status; END $$"
    )


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("SET lock_timeout = '5s'")
    op.execute("SET statement_timeout = '120s'")

    # The hot table first: a nullable column (catalog only), a concurrent partial index, and the
    # widened status CHECK.
    op.execute("ALTER TABLE webhook_deliveries ADD COLUMN IF NOT EXISTS tenant_id uuid")
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = '600s'")
        # A failed or cancelled CONCURRENTLY build leaves an INVALID index of the same name, which
        # IF NOT EXISTS would skip: drop it so this run (or a re-run) rebuilds it.
        # Offline rendering (the SQL lint) has no catalog to query.
        if not op.get_context().as_sql and op.get_bind().scalar(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM pg_index WHERE NOT indisvalid"
                " AND indexrelid = to_regclass('public.ix_wd_tenant'))"
            )
        ):
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_wd_tenant")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_wd_tenant ON webhook_deliveries "
            "(tenant_id) WHERE tenant_id IS NOT NULL"
        )
    _swap_status_check(add="ck_wd_status_v2", check=_NEW_STATUS, old="ck_wd_status")

    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute(_TABLE)
    op.execute(
        "CREATE UNIQUE INDEX ux_tenant_deletions_open ON tenant_deletions (tenant_id) "
        f"WHERE state {OPEN}"
    )
    op.execute(
        "CREATE INDEX ix_tenant_deletions_state_updated ON tenant_deletions (state, updated_at)"
    )
    # The tombstone lookup of tb_upsert_tenant.
    op.execute(
        "CREATE INDEX ix_tenant_deletions_done_key ON tenant_deletions (host, installation_id) "
        "WHERE state = 'done'"
    )
    # The app role only reads; the writes are the definer functions' (they run as the owner role).
    op.execute(_if_role(APP_ROLE, f"GRANT SELECT ON tenant_deletions TO {APP_ROLE};"))
    op.execute(
        _if_role(OWNER_ROLE, f"GRANT SELECT, INSERT, UPDATE ON tenant_deletions TO {OWNER_ROLE};")
    )

    # Same signatures and row types: only the bodies change. Created functions come next.
    for statement in (_LIST_FN, _GET_FN, _SET_FN, _UPSERT_FN, _REQUEST_FN, _DELETE_FN):
        op.execute(statement)
    _grant_functions((*CHANGED_FUNCTIONS, *NEW_FUNCTIONS))
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")


def downgrade() -> None:
    """Downgrade schema; refused while any tenant_deletions row exists."""
    op.execute("SET lock_timeout = '5s'")
    op.execute("SET statement_timeout = '120s'")
    # The records are kept as a compliance record: dropping the table would lose them, and the
    # previous release cannot represent a tenant that is being deleted.
    # Retry-safe: a re-run after a partial failure finds the table already dropped.
    op.execute(
        "DO $$ BEGIN IF to_regclass('public.tenant_deletions') IS NOT NULL THEN "
        "LOCK TABLE tenant_deletions IN ACCESS EXCLUSIVE MODE; END IF; END $$"
    )
    op.execute(
        "DO $$ BEGIN IF to_regclass('public.tenant_deletions') IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM tenant_deletions) THEN "
        "RAISE EXCEPTION 'cannot downgrade: tenant_deletions has rows; keep the compliance "
        "record' USING ERRCODE = '55000'; END IF; END $$"
    )
    # A `deleted` delivery cannot be represented by the previous status CHECK: refuse before
    # anything is dropped, so a refusal leaves the schema as it was.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM webhook_deliveries WHERE status = 'deleted') THEN "
        "RAISE EXCEPTION 'cannot downgrade: webhook_deliveries has rows with status deleted' "
        "USING ERRCODE = '55000'; END IF; END $$"
    )
    for signature in reversed(NEW_FUNCTIONS):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    for statement in (_OLD_LIST_FN, _OLD_GET_FN, _OLD_SET_FN, _OLD_UPSERT_FN):
        op.execute(statement)
    _grant_functions(CHANGED_FUNCTIONS)
    op.execute("-- squawk-ignore ban-drop-table\nDROP TABLE IF EXISTS tenant_deletions")
    # No row is `deleted` (checked above), so the validation cannot fail.
    op.execute(
        f"DO $$ BEGIN IF NOT {_has_constraint('ck_wd_status_v1')} THEN "
        "ALTER TABLE webhook_deliveries ADD CONSTRAINT ck_wd_status_v1 "
        f"CHECK ({_OLD_STATUS}) NOT VALID; END IF; END $$"
    )
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = '600s'")
        op.execute(
            "-- squawk-ignore constraint-missing-not-valid, prefer-robust-stmts\n"
            "ALTER TABLE webhook_deliveries VALIDATE CONSTRAINT ck_wd_status_v1"
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_wd_tenant")
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE webhook_deliveries DROP CONSTRAINT IF EXISTS ck_wd_status, "
        "DROP COLUMN IF EXISTS tenant_id"
    )
    op.execute(
        f"DO $$ BEGIN IF {_has_constraint('ck_wd_status_v1')} THEN "
        "ALTER TABLE webhook_deliveries RENAME CONSTRAINT ck_wd_status_v1 TO ck_wd_status; "
        "END IF; END $$"
    )
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
