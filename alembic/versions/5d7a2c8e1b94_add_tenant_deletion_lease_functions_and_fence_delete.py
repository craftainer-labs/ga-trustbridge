"""add the deletion lease and transition functions; fence tb_delete_tenant (NG6h-4a-1, ADR 0034)

Revision ID: 5d7a2c8e1b94
Revises: 3b8d6f2a4c17
Create Date: 2026-11-14 09:00:00.000000

The database half of the deletion engine (ADR 0034). Nothing in the application calls any of it yet,
so the previous release (N-1) is unaffected (NFR-0027): the table gains two columns with defaults
(catalog only) and every other change is a new function, except ``tb_delete_tenant``, which no
release calls and which changes signature (see below).

The app role keeps ``SELECT`` on ``tenant_deletions`` and gets ``EXECUTE`` on these ``SECURITY
DEFINER`` functions (owner role, ``search_path`` pinned to ``pg_catalog, public``); it cannot
write the
record any other way (NG6h-1 review (3)). The functions touch no ``audit_events``: the engine writes
the audit row in the same session transaction as the transition (decided, NG6h-4a split).

* Columns: ``attempts`` (consecutive failed attempts of the current step; reset by a successful
  transition) and ``retry_at`` (the backoff: a claim skips the row until it passes).
* ``tb_claim_tenant_deletion(holder, ttl_seconds)``: takes the lease of one non-terminal deletion
  whose lease is free or expired, whose ``not_before`` and ``retry_at`` have passed (a future
  ``not_before`` keeps it ``requested``, so a cancel works until then), and bumps ``fence``. It uses
  ``FOR UPDATE SKIP LOCKED``, so two reconcilers never claim the same row (ADR 0028
  invariants 9, 10).
* Every guarded function below takes ``(id, holder, fence)`` and proves the caller holds the live
  lease: the row is locked and ``holder`` and ``fence`` must match and ``expires_at`` must be in the
  future, otherwise ``TB003`` (``deletion lease lost``); a terminal or missing record is ``TB004``.
  ``tb_renew_tenant_deletion`` extends the lease; ``tb_release_tenant_deletion`` frees it;
  ``tb_retry_tenant_deletion`` records ``last_error``, counts the attempt, sets the backoff and
  frees
  the lease; ``tb_advance_tenant_deletion`` moves one step along ``requested`` > ``draining`` >
  ``platform`` > ``github`` > ``database`` (no skipping, no going back; the same state updates the
  cursor, counts and skipped only) and leaving ``requested`` needs a passed ``not_before`` and an
  admin-disabled tenant; ``tb_finish_tenant_deletion`` sets ``done`` (the tombstone) only from
  ``database`` and only after ``tb_delete_tenant`` removed the ``tenants`` row.
* ``tb_fail_tenant_deletion(id, holder, fence, reason)``: ``failed`` is terminal. With a lease it is
  the engine's explicit non-retryable error; with ``NULL`` holder and fence it is an operator action
  and needs no lease (a later write by the former holder then gets ``TB004``). It is refused in
  ``database`` once the tenant row is gone: that deletion can only finish, because ``failed`` is not
  a tombstone.
* ``tb_cancel_tenant_deletion(id)``: ``cancelled`` is terminal and valid only from ``requested``
  (outcomes ``cancelled``, ``not_cancellable``, ``not_found``). The tenant stays admin-disabled
  and a
  new request makes a new record (B3).
* ``tb_delete_tenant(uuid, text, bigint)`` replaces ``tb_delete_tenant(uuid)`` and checks the
  holder,
  the fence and the live lease of the deletion in its ``database`` step (NG6h-1 review (5)).

The upgrade fails loudly if the ``tb_delete_tenant`` it replaces is not the ``9c2e5a7f1d84`` body;
the downgrade restores that body (``tb_delete_tenant(uuid)``) and drops the rest.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5d7a2c8e1b94"
down_revision: str | Sequence[str] | None = "3b8d6f2a4c17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "trustbridge_app"
OWNER_ROLE = "trustbridge_owner"
TERMINAL = "('done', 'failed', 'cancelled')"
PREFIX = "LANGUAGE plpgsql SECURITY DEFINER\nSET search_path = pg_catalog, public"
NEW_FUNCTIONS = (
    "tb_claim_tenant_deletion(text, integer)",
    "tb_renew_tenant_deletion(uuid, text, bigint, integer)",
    "tb_advance_tenant_deletion(uuid, text, bigint, text, jsonb, jsonb, jsonb)",
    "tb_retry_tenant_deletion(uuid, text, bigint, text, integer)",
    "tb_release_tenant_deletion(uuid, text, bigint)",
    "tb_fail_tenant_deletion(uuid, text, bigint, text)",
    "tb_cancel_tenant_deletion(uuid)",
    "tb_finish_tenant_deletion(uuid, text, bigint, jsonb, jsonb)",
    "tb_delete_tenant(uuid, text, bigint)",
)
OLD_DELETE = "tb_delete_tenant(uuid)"


def _validate_lease(*, operator: bool = False) -> str:
    """PL/pgSQL validating the arguments, then locking the record and proving the live lease.

    With ``operator`` the lease is optional: ``NULL`` holder and fence together are the operator
    path; one without the other is invalid.
    """
    lease = """
    IF p_holder IS NULL AND p_fence IS NULL THEN
        v_operator := true;
    ELSIF p_holder IS NULL OR p_fence IS NULL OR length(p_holder) NOT BETWEEN 1 AND 255 THEN
        RAISE EXCEPTION 'invalid lease arguments' USING ERRCODE = '22023';
    END IF;"""
    if not operator:
        lease = """
    IF p_holder IS NULL OR p_fence IS NULL OR length(p_holder) NOT BETWEEN 1 AND 255 THEN
        RAISE EXCEPTION 'invalid lease arguments' USING ERRCODE = '22023';
    END IF;"""
    check = f"""
    SELECT * INTO v_d FROM public.tenant_deletions d WHERE d.id = p_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'deletion not found' USING ERRCODE = 'TB004';
    END IF;
    IF v_d.state IN {TERMINAL} THEN
        RAISE EXCEPTION 'deletion is terminal' USING ERRCODE = 'TB004';
    END IF;
    IF NOT v_operator AND (v_d.holder IS DISTINCT FROM p_holder
       OR v_d.fence IS DISTINCT FROM p_fence
       OR v_d.expires_at IS NULL OR v_d.expires_at <= now()) THEN
        RAISE EXCEPTION 'deletion lease lost' USING ERRCODE = 'TB003';
    END IF;"""  # noqa: S608 -- constants only
    return lease + check


_DECLARE = "    v_d public.tenant_deletions%ROWTYPE;\n    v_operator boolean := false;"
_FREE = "holder = NULL, expires_at = NULL"

_CLAIM_FN = f"""
CREATE FUNCTION tb_claim_tenant_deletion(p_holder text, p_ttl_seconds integer)
RETURNS SETOF public.tenant_deletions
{PREFIX}
AS $$
DECLARE
    v_row public.tenant_deletions%ROWTYPE;
BEGIN
    IF p_holder IS NULL OR length(p_holder) NOT BETWEEN 1 AND 255
       OR p_ttl_seconds IS NULL OR p_ttl_seconds NOT BETWEEN 1 AND 3600 THEN
        RAISE EXCEPTION 'invalid claim arguments' USING ERRCODE = '22023';
    END IF;
    UPDATE public.tenant_deletions d
       SET holder = p_holder, fence = d.fence + 1,
           expires_at = now() + pg_catalog.make_interval(secs => p_ttl_seconds),
           updated_at = now()
     WHERE d.id = (
        SELECT c.id FROM public.tenant_deletions c
         WHERE c.state NOT IN {TERMINAL}
           AND (c.expires_at IS NULL OR c.expires_at <= now())
           AND (c.not_before IS NULL OR c.not_before <= now())
           AND (c.retry_at IS NULL OR c.retry_at <= now())
         ORDER BY c.updated_at, c.id LIMIT 1 FOR UPDATE SKIP LOCKED)
    RETURNING d.* INTO v_row;
    IF FOUND THEN
        RETURN NEXT v_row;
    END IF;
END
$$
"""  # noqa: S608 -- constants only

_RENEW_FN = f"""
CREATE FUNCTION tb_renew_tenant_deletion(
    p_id uuid, p_holder text, p_fence bigint, p_ttl_seconds integer
) RETURNS timestamptz
{PREFIX}
AS $$
DECLARE
{_DECLARE}
    v_expires timestamptz;
BEGIN
    IF p_ttl_seconds IS NULL OR p_ttl_seconds NOT BETWEEN 1 AND 3600 THEN
        RAISE EXCEPTION 'invalid ttl' USING ERRCODE = '22023';
    END IF;{_validate_lease()}
    UPDATE public.tenant_deletions d
       SET expires_at = now() + pg_catalog.make_interval(secs => p_ttl_seconds), updated_at = now()
     WHERE d.id = p_id RETURNING d.expires_at INTO v_expires;
    RETURN v_expires;
END
$$
"""  # noqa: S608 -- constants only

_ADVANCE_FN = f"""
CREATE FUNCTION tb_advance_tenant_deletion(
    p_id uuid, p_holder text, p_fence bigint, p_to text,
    p_cursor jsonb DEFAULT NULL, p_counts jsonb DEFAULT NULL, p_skipped jsonb DEFAULT NULL
) RETURNS void
{PREFIX}
AS $$
DECLARE
{_DECLARE}
    v_next text;
    v_disabled timestamptz;
    v_found boolean;
BEGIN
    IF (p_cursor IS NOT NULL AND pg_catalog.jsonb_typeof(p_cursor) <> 'object')
       OR (p_counts IS NOT NULL AND pg_catalog.jsonb_typeof(p_counts) <> 'object')
       OR (p_skipped IS NOT NULL AND pg_catalog.jsonb_typeof(p_skipped) <> 'array') THEN
        RAISE EXCEPTION 'invalid step data' USING ERRCODE = '22023';
    END IF;{_validate_lease()}
    v_next := CASE v_d.state
        WHEN 'requested' THEN 'draining' WHEN 'draining' THEN 'platform'
        WHEN 'platform' THEN 'github' WHEN 'github' THEN 'database' END;
    IF p_to IS DISTINCT FROM v_d.state AND p_to IS DISTINCT FROM v_next THEN
        RAISE EXCEPTION 'invalid deletion transition' USING ERRCODE = 'TB004';
    END IF;
    IF v_d.state = 'requested' AND p_to = 'draining' THEN
        IF v_d.not_before IS NOT NULL AND v_d.not_before > now() THEN
            RAISE EXCEPTION 'the deletion grace window has not elapsed' USING ERRCODE = '55000';
        END IF;
        SELECT t.admin_disabled_at INTO v_disabled FROM public.tenants t
         WHERE t.id = v_d.tenant_id;
        v_found := FOUND;
        IF v_found AND v_disabled IS NULL THEN
            RAISE EXCEPTION 'tenant is not admin-disabled' USING ERRCODE = '55000';
        END IF;
    END IF;
    UPDATE public.tenant_deletions d
       SET state = p_to,
           step_cursor = COALESCE(p_cursor, CASE WHEN p_to <> d.state THEN '{{}}'::jsonb
                                                 ELSE d.step_cursor END),
           counts = COALESCE(p_counts, d.counts),
           skipped = COALESCE(p_skipped, d.skipped),
           attempts = 0, retry_at = NULL, last_error = NULL, updated_at = now()
     WHERE d.id = p_id;
END
$$
"""  # noqa: S608 -- constants only

_RETRY_FN = f"""
CREATE FUNCTION tb_retry_tenant_deletion(
    p_id uuid, p_holder text, p_fence bigint, p_error text, p_delay_seconds integer
) RETURNS integer
{PREFIX}
AS $$
DECLARE
{_DECLARE}
    v_attempts integer;
BEGIN
    IF p_error IS NULL OR length(p_error) = 0
       OR p_delay_seconds IS NULL OR p_delay_seconds NOT BETWEEN 0 AND 86400 THEN
        RAISE EXCEPTION 'invalid retry arguments' USING ERRCODE = '22023';
    END IF;{_validate_lease()}
    UPDATE public.tenant_deletions d
       SET attempts = d.attempts + 1, last_error = pg_catalog.left(p_error, 1000),
           retry_at = now() + pg_catalog.make_interval(secs => p_delay_seconds),
           {_FREE}, updated_at = now()
     WHERE d.id = p_id RETURNING d.attempts INTO v_attempts;
    RETURN v_attempts;
END
$$
"""  # noqa: S608 -- constants only

_RELEASE_FN = f"""
CREATE FUNCTION tb_release_tenant_deletion(p_id uuid, p_holder text, p_fence bigint)
RETURNS void
{PREFIX}
AS $$
DECLARE
{_DECLARE}
BEGIN{_validate_lease()}
    UPDATE public.tenant_deletions d SET {_FREE}, updated_at = now() WHERE d.id = p_id;
END
$$
"""  # noqa: S608 -- constants only

_FAIL_FN = f"""
CREATE FUNCTION tb_fail_tenant_deletion(p_id uuid, p_holder text, p_fence bigint, p_reason text)
RETURNS void
{PREFIX}
AS $$
DECLARE
{_DECLARE}
BEGIN
    IF p_reason IS NULL OR length(p_reason) = 0 THEN
        RAISE EXCEPTION 'a reason is required' USING ERRCODE = '22023';
    END IF;{_validate_lease(operator=True)}
    IF v_d.state = 'database' AND NOT EXISTS (
        SELECT 1 FROM public.tenants t WHERE t.id = v_d.tenant_id
    ) THEN
        RAISE EXCEPTION 'the tenant row is gone: the deletion can only finish'
            USING ERRCODE = 'TB004';
    END IF;
    UPDATE public.tenant_deletions d
       SET state = 'failed', last_error = pg_catalog.left(p_reason, 1000), retry_at = NULL,
           {_FREE}, updated_at = now()
     WHERE d.id = p_id;
END
$$
"""  # noqa: S608 -- constants only

_CANCEL_FN = f"""
CREATE FUNCTION tb_cancel_tenant_deletion(p_id uuid) RETURNS text
{PREFIX}
AS $$
DECLARE
    v_d public.tenant_deletions%ROWTYPE;
BEGIN
    SELECT * INTO v_d FROM public.tenant_deletions d WHERE d.id = p_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN 'not_found';
    END IF;
    IF v_d.state = 'cancelled' THEN
        RETURN 'cancelled';
    END IF;
    IF v_d.state <> 'requested' THEN
        RETURN 'not_cancellable';
    END IF;
    UPDATE public.tenant_deletions d
       SET state = 'cancelled', retry_at = NULL, {_FREE}, updated_at = now()
     WHERE d.id = p_id;
    RETURN 'cancelled';
END
$$
"""  # noqa: S608 -- constants only

_FINISH_FN = f"""
CREATE FUNCTION tb_finish_tenant_deletion(
    p_id uuid, p_holder text, p_fence bigint,
    p_counts jsonb DEFAULT NULL, p_skipped jsonb DEFAULT NULL
) RETURNS void
{PREFIX}
AS $$
DECLARE
{_DECLARE}
BEGIN
    IF (p_counts IS NOT NULL AND pg_catalog.jsonb_typeof(p_counts) <> 'object')
       OR (p_skipped IS NOT NULL AND pg_catalog.jsonb_typeof(p_skipped) <> 'array') THEN
        RAISE EXCEPTION 'invalid step data' USING ERRCODE = '22023';
    END IF;{_validate_lease()}
    IF v_d.state <> 'database' THEN
        RAISE EXCEPTION 'only the database step can finish' USING ERRCODE = 'TB004';
    END IF;
    IF EXISTS (SELECT 1 FROM public.tenants t WHERE t.id = v_d.tenant_id) THEN
        RAISE EXCEPTION 'the tenant row still exists' USING ERRCODE = 'TB004';
    END IF;
    -- `done` is the tombstone of (host, installation_id) (tb_upsert_tenant, SqlTenants).
    UPDATE public.tenant_deletions d
       SET state = 'done', step_cursor = '{{}}'::jsonb, counts = COALESCE(p_counts, d.counts),
           skipped = COALESCE(p_skipped, d.skipped), retry_at = NULL, last_error = NULL,
           {_FREE}, updated_at = now()
     WHERE d.id = p_id;
END
$$
"""  # noqa: S608 -- constants only

_PREVIOUS_DELETE_FN = """
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
    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('tb_tenant:' || v_host || ':' || v_inst::text, 0));
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
"""  # noqa: E501, W293 -- the 9c2e5a7f1d84 body, verbatim


def _replaced(body: str, old: str, new: str) -> str:
    """`body` with `old` replaced by `new`; anything but exactly one match is an error."""
    if body.count(old) != 1:
        raise ValueError(f"expected text not found exactly once in the function body: {old[:60]!r}")
    return body.replace(old, new)


_DELETE_FN = _replaced(
    _replaced(
        _replaced(
            _PREVIOUS_DELETE_FN,
            "CREATE FUNCTION tb_delete_tenant(p_id uuid) RETURNS void",
            "CREATE FUNCTION tb_delete_tenant(p_id uuid, p_holder text, p_fence bigint) "
            "RETURNS void",
        ),
        "    v_not_before timestamptz;\n",
        "    v_not_before timestamptz;\n    v_holder text;\n    v_fence bigint;\n"
        "    v_expires timestamptz;\n",
    ),
    "    -- `database` is a non-terminal (running) state. NG6h-4a adds the holder and fence "
    "arguments\n"
    """    -- and checks them here (the columns exist, but nothing passes them yet).
    SELECT d.not_before INTO v_not_before FROM public.tenant_deletions d
     WHERE d.tenant_id = p_id AND d.state = 'database';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'tenant has no deletion in its database step' USING ERRCODE = '55000';
    END IF;
""",
    """    -- `database` is a non-terminal (running) state; the caller must hold its live lease
    -- (holder, fence and an unexpired `expires_at`). The deletion row is locked after the tenant
    -- row, an order no other function reverses.
    SELECT d.not_before, d.holder, d.fence, d.expires_at
      INTO v_not_before, v_holder, v_fence, v_expires FROM public.tenant_deletions d
     WHERE d.tenant_id = p_id AND d.state = 'database' FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'tenant has no deletion in its database step' USING ERRCODE = '55000';
    END IF;
    IF p_holder IS NULL OR p_fence IS NULL OR v_holder IS DISTINCT FROM p_holder
       OR v_fence IS DISTINCT FROM p_fence OR v_expires IS NULL OR v_expires <= now() THEN
        RAISE EXCEPTION 'deletion lease lost' USING ERRCODE = 'TB003';
    END IF;
""",
)


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


_ASSERT_PREVIOUS = (
    "DO $$ BEGIN IF to_regprocedure('tb_delete_tenant(uuid)') IS NULL OR "
    "pg_get_functiondef(to_regprocedure('tb_delete_tenant(uuid)')) "
    "NOT LIKE '%NG6h-4a adds the holder and fence arguments%' THEN "
    "RAISE EXCEPTION 'tb_delete_tenant(uuid) is not the 9c2e5a7f1d84 body' "
    "USING ERRCODE = '55000'; END IF; END $$"
)
_ASSERT_NEW = (
    "DO $$ BEGIN IF to_regprocedure('tb_delete_tenant(uuid, text, bigint)') IS NULL OR "
    "pg_get_functiondef(to_regprocedure('tb_delete_tenant(uuid, text, bigint)')) "
    "NOT LIKE '%deletion lease lost%' THEN "
    "RAISE EXCEPTION 'tb_delete_tenant(uuid, text, bigint) is not the 5d7a2c8e1b94 body' "
    "USING ERRCODE = '55000'; END IF; END $$"
)


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("SET lock_timeout = '5s'")
    op.execute("SET statement_timeout = '120s'")
    # Catalog-only (constant defaults): the backoff state of a step.
    op.execute(
        "ALTER TABLE tenant_deletions ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0"
    )
    op.execute("ALTER TABLE tenant_deletions ADD COLUMN IF NOT EXISTS retry_at timestamptz")
    op.execute(_ASSERT_PREVIOUS)
    # No release calls tb_delete_tenant (the engine does not exist yet): the signature may change.
    op.execute(f"DROP FUNCTION {OLD_DELETE}")
    for statement in (
        _CLAIM_FN,
        _RENEW_FN,
        _ADVANCE_FN,
        _RETRY_FN,
        _RELEASE_FN,
        _FAIL_FN,
        _CANCEL_FN,
        _FINISH_FN,
        _DELETE_FN,
    ):
        op.execute(statement)
    _grant_functions(NEW_FUNCTIONS)
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")


def downgrade() -> None:
    """Downgrade schema: the 9c2e5a7f1d84 `tb_delete_tenant(uuid)` comes back."""
    op.execute("SET lock_timeout = '5s'")
    op.execute("SET statement_timeout = '120s'")
    op.execute(_ASSERT_NEW)
    for signature in reversed(NEW_FUNCTIONS):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.execute(_PREVIOUS_DELETE_FN)
    _grant_functions((OLD_DELETE,))
    op.execute(
        "-- squawk-ignore ban-drop-column\n"
        "ALTER TABLE tenant_deletions "
        "DROP COLUMN IF EXISTS attempts, DROP COLUMN IF EXISTS retry_at"
    )
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
