"""pin pg_temp on the definers, reset retry state on progress, delivery backstop (NG6h-4a-2)

Revision ID: 6f3b9d2e4a85
Revises: 5d7a2c8e1b94
Create Date: 2026-11-13 20:00:00.000000

Three expand-only changes to the definer functions of the deletion engine (ADR 0034, ADR 0031);
no table, column or signature changes, so the previous release (N-1) is unaffected (NFR-0027).

1. ``search_path`` gains ``pg_temp``. Every ``SECURITY DEFINER`` function created before the engine
   migration pinned ``pg_catalog, public``. The PostgreSQL documentation ("Writing SECURITY DEFINER
   Functions Safely") says a ``pg_temp`` not listed explicitly is searched *first* for relations,
   so a caller who can create temporary tables could shadow ``public.tenants`` for the owner role.
   ``ALTER FUNCTION ... SET search_path`` (no body is replaced) puts ``pg_temp`` last on each of
   the nine older definers, enumerated below with exact signatures. The functions of
   ``5d7a2c8e1b94`` already carry it. An integration test asserts it for every definer in the
   schema, so a new one without it fails CI.
2. ``tb_advance_tenant_deletion``: a same-state advance whose step cursor *changes* is real
   progress and also resets ``attempts``, ``retry_at`` and ``last_error`` (as a step change does).
   A same-state save with an unchanged (or no) cursor still leaves them alone, so a poison step
   keeps counting its failures up.
3. ``tb_delete_tenant``: the emptiness check (before and after the delete) also requires that the
   tenant has no ``webhook_deliveries`` row whose ``status`` is not ``deleted``: an erasure backstop
   for the ``DatabaseStep``, which clears the bodies and marks the rows ``deleted``. The lock order
   (advisory lock, deletion row, ``tenants`` row) is unchanged.

Both replaced bodies are checked before they are replaced (the upgrade fails loudly if it is not
the ``5d7a2c8e1b94`` body). The downgrade refuses (``55000``) while any non-terminal deletion
exists, checks that the bodies are this revision's, restores the ``5d7a2c8e1b94`` ones and puts the
older definers' ``search_path`` back to ``pg_catalog, public``.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6f3b9d2e4a85"
down_revision: str | Sequence[str] | None = "5d7a2c8e1b94"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TERMINAL = "('done', 'failed', 'cancelled')"
ADVANCE = "tb_advance_tenant_deletion(uuid, text, bigint, text, jsonb, jsonb, jsonb)"
DELETE = "tb_delete_tenant(uuid, text, bigint)"
# The definers created before 5d7a2c8e1b94 (those since carry pg_temp): exact signatures.
OLDER_DEFINERS = (
    "tb_resolve_tenant(text, bigint)",
    "tb_upsert_tenant(text, bigint, text, bigint, text)",
    "tb_list_tenants()",
    "tb_get_tenant(uuid)",
    "tb_set_tenant_disabled(uuid, text, text, boolean)",
    "tb_request_tenant_deletion(uuid, text, bigint, text, text, text, boolean, timestamptz)",
    "tb_audit_purge(timestamptz, integer, integer)",
    "tb_audit_list(uuid, bigint, bigint, timestamptz, timestamptz, text, text, text, text, "
    "text, double precision, boolean, integer)",
    "tb_audit_get(uuid)",
)

# The 5d7a2c8e1b94 bodies, restored by the downgrade (with CREATE OR REPLACE, so the owner and
# the grants stay).
_PREVIOUS_ADVANCE_FN = """
CREATE OR REPLACE FUNCTION tb_advance_tenant_deletion(
    p_id uuid, p_holder text, p_fence bigint, p_to text,
    p_cursor jsonb DEFAULT NULL, p_counts jsonb DEFAULT NULL, p_skipped jsonb DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    v_d public.tenant_deletions%ROWTYPE;
    v_next text;
    v_disabled timestamptz;
BEGIN
    IF (p_cursor IS NOT NULL AND pg_catalog.jsonb_typeof(p_cursor) <> 'object')
       OR (p_counts IS NOT NULL AND pg_catalog.jsonb_typeof(p_counts) <> 'object')
       OR (p_skipped IS NOT NULL AND pg_catalog.jsonb_typeof(p_skipped) <> 'array') THEN
        RAISE EXCEPTION 'invalid step data' USING ERRCODE = '22023';
    END IF;
    IF p_holder IS NULL OR p_fence IS NULL OR length(p_holder) NOT BETWEEN 1 AND 255 THEN
        RAISE EXCEPTION 'invalid lease arguments' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_d FROM public.tenant_deletions d WHERE d.id = p_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'deletion not found' USING ERRCODE = 'TB004';
    END IF;
    IF v_d.state IN ('done', 'failed', 'cancelled') THEN
        RAISE EXCEPTION 'deletion is terminal' USING ERRCODE = 'TB004';
    END IF;
    IF v_d.holder IS DISTINCT FROM p_holder
       OR v_d.fence IS DISTINCT FROM p_fence
       OR v_d.expires_at IS NULL OR v_d.expires_at <= pg_catalog.clock_timestamp() THEN
        RAISE EXCEPTION 'deletion lease lost' USING ERRCODE = 'TB003';
    END IF;
    v_next := CASE v_d.state
        WHEN 'requested' THEN 'draining' WHEN 'draining' THEN 'platform'
        WHEN 'platform' THEN 'github' WHEN 'github' THEN 'database' END;
    IF p_to IS NULL OR (p_to <> v_d.state AND p_to IS DISTINCT FROM v_next) THEN
        RAISE EXCEPTION 'invalid deletion transition' USING ERRCODE = 'TB004';
    END IF;
    IF v_d.state = 'requested' AND p_to = 'draining' THEN
        IF v_d.not_before IS NOT NULL AND v_d.not_before > pg_catalog.clock_timestamp() THEN
            RAISE EXCEPTION 'the deletion grace window has not elapsed' USING ERRCODE = '55000';
        END IF;
        SELECT t.admin_disabled_at INTO v_disabled FROM public.tenants t
         WHERE t.id = v_d.tenant_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'the tenant row is gone: the deletion cannot leave requested'
                USING ERRCODE = '55000';
        END IF;
        IF v_disabled IS NULL THEN
            RAISE EXCEPTION 'tenant is not admin-disabled' USING ERRCODE = '55000';
        END IF;
    END IF;
    UPDATE public.tenant_deletions d
       SET state = p_to,
           step_cursor = COALESCE(p_cursor, CASE WHEN p_to <> d.state THEN '{}'::jsonb
                                                 ELSE d.step_cursor END),
           counts = COALESCE(p_counts, d.counts),
           skipped = COALESCE(p_skipped, d.skipped),
           attempts = CASE WHEN p_to <> d.state THEN 0 ELSE d.attempts END,
           retry_at = CASE WHEN p_to <> d.state THEN NULL ELSE d.retry_at END,
           last_error = CASE WHEN p_to <> d.state THEN NULL ELSE d.last_error END,
           updated_at = now()
     WHERE d.id = p_id;
END
$$
"""

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
         THEN 'credential_operations' END)
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
         THEN 'credential_operations' END)
 INTO v_left;
    IF v_left <> '' THEN
        RAISE EXCEPTION 'tenant still has rows in: %', v_left USING ERRCODE = '55000';
    END IF;
END
$$
"""  # noqa: E501 -- the 5d7a2c8e1b94 body, verbatim


def _replaced(body: str, old: str, new: str, count: int = 1) -> str:
    """`body` with `old` replaced by `new`; any other number of matches is an error."""
    if body.count(old) != count:
        raise ValueError(
            f"expected text not found {count} time(s) in the function body: {old[:60]!r}"
        )
    return body.replace(old, new)


_UPDATE = "    UPDATE public.tenant_deletions d\n       SET state = p_to,"
_PROGRESS = """    -- Real progress (a new step, or a cursor that moved) clears the retry state; a
    -- same-state save with an unchanged cursor (or none) leaves it alone: a poison step keeps
    -- counting its failures.
    v_progress := p_to <> v_d.state
                  OR (p_cursor IS NOT NULL AND p_cursor IS DISTINCT FROM v_d.step_cursor);
"""
_ADVANCE_FN = _PREVIOUS_ADVANCE_FN
for _old, _new in (
    ("    v_disabled timestamptz;\n", "    v_disabled timestamptz;\n    v_progress boolean;\n"),
    (_UPDATE, _PROGRESS + _UPDATE),
    ("attempts = CASE WHEN p_to <> d.state THEN", "attempts = CASE WHEN v_progress THEN"),
    ("retry_at = CASE WHEN p_to <> d.state THEN", "retry_at = CASE WHEN v_progress THEN"),
    ("last_error = CASE WHEN p_to <> d.state THEN", "last_error = CASE WHEN v_progress THEN"),
):
    _ADVANCE_FN = _replaced(_ADVANCE_FN, _old, _new)

_LAST_TABLE = """    CASE WHEN EXISTS (SELECT 1 FROM public.credential_operations WHERE tenant_id = p_id)
         THEN 'credential_operations' END)
"""  # noqa: E501 -- a line of the 5d7a2c8e1b94 body
_DELIVERIES = """    CASE WHEN EXISTS (SELECT 1 FROM public.credential_operations WHERE tenant_id = p_id)
         THEN 'credential_operations' END,
    CASE WHEN EXISTS (SELECT 1 FROM public.webhook_deliveries
                      WHERE tenant_id = p_id AND status <> 'deleted')
         THEN 'webhook_deliveries' END)
"""  # noqa: E501 -- the same line, plus the new check
# The list appears twice: before the delete and in the re-check after it.
_DELETE_FN = _replaced(_PREVIOUS_DELETE_FN, _LAST_TABLE, _DELIVERIES, count=2)


def _assert_body(signature: str, present: str, absent: str | None, source: str) -> str:
    """A DO block failing (55000) unless the function's body has `present` and lacks `absent`."""
    definition = f"pg_get_functiondef(to_regprocedure('{signature}'))"
    wrong = f"{definition} NOT LIKE '%{present}%'"
    if absent is not None:
        wrong += f" OR {definition} LIKE '%{absent}%'"
    return (
        f"DO $$ BEGIN IF to_regprocedure('{signature}') IS NULL OR {wrong} THEN "
        f"RAISE EXCEPTION '{signature} is not the {source} body' "
        "USING ERRCODE = '55000'; END IF; END $$"
    )


_ASSERT_PREVIOUS = (
    _assert_body(
        ADVANCE, "attempts = CASE WHEN p_to <> d.state THEN 0", "v_progress", "5d7a2c8e1b94"
    ),
    _assert_body(DELETE, "deletion lease lost", "webhook_deliveries", "5d7a2c8e1b94"),
)
_ASSERT_NEW = (
    _assert_body(ADVANCE, "v_progress", "attempts = CASE WHEN p_to <> d.state", "6f3b9d2e4a85"),
    _assert_body(DELETE, "webhook_deliveries", None, "6f3b9d2e4a85"),
)
_REFUSE_LIVE_DELETIONS = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM tenant_deletions "  # noqa: S608 -- constants only
    f"WHERE state NOT IN {TERMINAL}) THEN "
    "RAISE EXCEPTION 'non-terminal tenant deletions exist: let them finish or cancel them "
    "before downgrading' USING ERRCODE = '55000'; END IF; END $$"
)


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    for statement in _ASSERT_PREVIOUS:
        op.execute(statement)
    # ALTER FUNCTION replaces no body; it takes a brief lock on the function's catalog row.
    for signature in OLDER_DEFINERS:
        op.execute(f"ALTER FUNCTION {signature} SET search_path = pg_catalog, public, pg_temp")
    # Same signatures and return types, so the owner and the grants stay.
    op.execute(_ADVANCE_FN)
    op.execute(_DELETE_FN)


def downgrade() -> None:
    """Downgrade schema: the 5d7a2c8e1b94 bodies and the older search paths come back."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    # Closes the window in which a request could commit between the guard and the restore.
    op.execute("LOCK TABLE tenant_deletions IN SHARE ROW EXCLUSIVE MODE")
    op.execute(_REFUSE_LIVE_DELETIONS)
    for statement in _ASSERT_NEW:
        op.execute(statement)
    op.execute(_PREVIOUS_ADVANCE_FN)
    op.execute(_PREVIOUS_DELETE_FN)
    for signature in OLDER_DEFINERS:
        op.execute(f"ALTER FUNCTION {signature} SET search_path = pg_catalog, public")
