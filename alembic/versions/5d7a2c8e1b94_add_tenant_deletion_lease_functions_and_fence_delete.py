"""add the deletion lease and transition functions; fence tb_delete_tenant (NG6h-4a-1, ADR 0034)

Revision ID: 5d7a2c8e1b94
Revises: 3b8d6f2a4c17
Create Date: 2026-11-14 09:00:00.000000

The database half of the deletion engine (ADR 0034). Nothing in the application calls any of it yet,
so the previous release (N-1) is unaffected (NFR-0027): the table gains two columns with defaults
(catalog only) and every other change is a new function, except ``tb_delete_tenant``, which no
release calls and which changes signature (see below).

The app role keeps ``SELECT`` on ``tenant_deletions`` and gets ``EXECUTE`` on these ``SECURITY
DEFINER`` functions (owner role, ``search_path`` pinned to ``pg_catalog, public, pg_temp`` as the
PostgreSQL
documentation advises); it cannot
write the
record any other way (NG6h-1 review (3)). The functions touch no ``audit_events``: the engine writes
the audit row in the same session transaction as the transition (decided, NG6h-4a split).

* Columns: ``attempts`` (bigint; consecutive failed attempts of the current step; reset by a step
  change) and ``retry_at`` (the backoff: a claim skips the row until it passes).
* ``tb_claim_tenant_deletion(holder, ttl_seconds)``: takes the lease of one non-terminal deletion
  whose lease is free or expired, whose ``not_before`` and ``retry_at`` have passed (a future
  ``not_before`` keeps it ``requested``, so a cancel works until then), and bumps ``fence``. It uses
  ``FOR UPDATE SKIP LOCKED``, so two reconcilers never claim the same row (ADR 0028
  invariants 9, 10).
* Every guarded function below takes ``(id, holder, fence)`` and proves the caller holds the live
  lease: the row is locked and ``holder`` and ``fence`` must match and ``expires_at`` must be in the
  future by the wall clock (``clock_timestamp()``, not the transaction's ``now()``), otherwise
  ``TB003`` (``deletion lease lost``); a terminal or missing record is ``TB004``.
  ``tb_renew_tenant_deletion`` extends the lease; ``tb_release_tenant_deletion`` frees it;
  ``tb_retry_tenant_deletion`` records ``last_error``, counts the attempt, sets the backoff and
  frees
  the lease; ``tb_advance_tenant_deletion`` moves one step along ``requested`` > ``draining`` >
  ``platform`` > ``github`` > ``database`` (no skipping, no going back; the same state updates the
  cursor, counts and skipped only and leaves ``attempts``, ``retry_at`` and ``last_error`` alone)
  and leaving ``requested`` needs a passed ``not_before`` and an
  admin-disabled tenant that exists (a missing ``tenants`` row refuses with ``55000``);
  ``tb_finish_tenant_deletion`` sets ``done`` (the tombstone) only from
  ``database`` and only after ``tb_delete_tenant`` removed the ``tenants`` row.
* ``tb_fail_tenant_deletion(id, holder, fence, reason)``: the engine's explicit non-retryable
  error, with a live lease like every guarded function (no ``NULL`` path). ``failed`` is terminal.
  It
  is refused in ``database`` once the tenant row is gone: that deletion can only finish, because
  ``failed`` is not a tombstone.
* ``tb_operator_fail_tenant_deletion(id, actor, reason)``: the operator's way to fail a stuck
  deletion, with no lease of its own. It is refused with ``TB003`` while a live lease is held and
  with
  ``TB004`` for a missing or terminal record (or a ``database`` step whose tenant row is gone); the
  actor is recorded in ``last_error`` (``operator <actor>: <reason>``). ``trustbridge_app`` is the
  only runtime role (ADR 0031) and serves the admin API too, so it gets ``EXECUTE`` on this
  function;
  the caller (the admin API) authorises the operator and supplies the actor.
* ``tb_cancel_tenant_deletion(id)``: ``cancelled`` is terminal and valid only from ``requested``
  (outcomes ``cancelled``, ``not_cancellable``, ``not_found``). The tenant stays admin-disabled
  and a
  new request makes a new record (B3).
* ``tb_delete_tenant(uuid, text, bigint)`` replaces ``tb_delete_tenant(uuid)`` and checks the
  holder,
  the fence and the live lease of the deletion in its ``database`` step (NG6h-1 review (5)).

The upgrade fails loudly if the ``tb_delete_tenant`` it replaces is not the ``9c2e5a7f1d84`` body;
the downgrade refuses (``55000``) while any non-terminal deletion exists, then restores that body
(``tb_delete_tenant(uuid)``) and drops the rest.

Lock order, in every function taking more than one lock: the advisory lock, then the deletion row,
then the ``tenants`` row (only ``tb_delete_tenant`` locks the latter).
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
PREFIX = "LANGUAGE plpgsql SECURITY DEFINER\nSET search_path = pg_catalog, public, pg_temp"
NEW_FUNCTIONS = (
    "tb_claim_tenant_deletion(text, integer)",
    "tb_renew_tenant_deletion(uuid, text, bigint, integer)",
    "tb_advance_tenant_deletion(uuid, text, bigint, text, jsonb, jsonb, jsonb)",
    "tb_retry_tenant_deletion(uuid, text, bigint, text, integer)",
    "tb_release_tenant_deletion(uuid, text, bigint)",
    "tb_fail_tenant_deletion(uuid, text, bigint, text)",
    "tb_operator_fail_tenant_deletion(uuid, text, text)",
    "tb_cancel_tenant_deletion(uuid)",
    "tb_finish_tenant_deletion(uuid, text, bigint, jsonb, jsonb)",
    "tb_delete_tenant(uuid, text, bigint)",
)
OLD_DELETE = "tb_delete_tenant(uuid)"


def _validate_lease() -> str:
    """PL/pgSQL validating the arguments, then locking the record and proving the live lease.

    The liveness check uses ``clock_timestamp()``, not ``now()``: ``now()`` is the start of the
    transaction, which a long step can outlive.
    """
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
    IF v_d.holder IS DISTINCT FROM p_holder
       OR v_d.fence IS DISTINCT FROM p_fence
       OR v_d.expires_at IS NULL OR v_d.expires_at <= pg_catalog.clock_timestamp() THEN
        RAISE EXCEPTION 'deletion lease lost' USING ERRCODE = 'TB003';
    END IF;"""  # noqa: S608 -- constants only
    return lease + check


_DECLARE = "    v_d public.tenant_deletions%ROWTYPE;"
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
           expires_at = pg_catalog.clock_timestamp()
                        + pg_catalog.make_interval(secs => p_ttl_seconds),
           updated_at = now()
     WHERE d.id = (
        SELECT c.id FROM public.tenant_deletions c
         WHERE c.state NOT IN {TERMINAL}
           AND (c.expires_at IS NULL OR c.expires_at <= pg_catalog.clock_timestamp())
           AND (c.not_before IS NULL OR c.not_before <= pg_catalog.clock_timestamp())
           AND (c.retry_at IS NULL OR c.retry_at <= pg_catalog.clock_timestamp())
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
       SET expires_at = GREATEST(d.expires_at, pg_catalog.clock_timestamp()
                                 + pg_catalog.make_interval(secs => p_ttl_seconds)),
           updated_at = now()
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
BEGIN
    IF (p_cursor IS NOT NULL AND pg_catalog.jsonb_typeof(p_cursor) <> 'object')
       OR (p_counts IS NOT NULL AND pg_catalog.jsonb_typeof(p_counts) <> 'object')
       OR (p_skipped IS NOT NULL AND pg_catalog.jsonb_typeof(p_skipped) <> 'array') THEN
        RAISE EXCEPTION 'invalid step data' USING ERRCODE = '22023';
    END IF;{_validate_lease()}
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
           step_cursor = COALESCE(p_cursor, CASE WHEN p_to <> d.state THEN '{{}}'::jsonb
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
"""  # noqa: S608 -- constants only

_RETRY_FN = f"""
CREATE FUNCTION tb_retry_tenant_deletion(
    p_id uuid, p_holder text, p_fence bigint, p_error text, p_delay_seconds integer
) RETURNS bigint
{PREFIX}
AS $$
DECLARE
{_DECLARE}
    v_attempts bigint;
BEGIN
    IF p_error IS NULL OR length(p_error) = 0
       OR p_delay_seconds IS NULL OR p_delay_seconds NOT BETWEEN 0 AND 86400 THEN
        RAISE EXCEPTION 'invalid retry arguments' USING ERRCODE = '22023';
    END IF;{_validate_lease()}
    UPDATE public.tenant_deletions d
       SET attempts = d.attempts + 1, last_error = pg_catalog.left(p_error, 1000),
           retry_at = pg_catalog.clock_timestamp()
                      + pg_catalog.make_interval(secs => p_delay_seconds),
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
    END IF;{_validate_lease()}
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

_OPERATOR_FAIL_FN = f"""
CREATE FUNCTION tb_operator_fail_tenant_deletion(p_id uuid, p_actor text, p_reason text)
RETURNS void
{PREFIX}
AS $$
DECLARE
{_DECLARE}
BEGIN
    IF p_actor IS NULL OR length(p_actor) NOT BETWEEN 1 AND 255
       OR p_reason IS NULL OR length(p_reason) = 0 THEN
        RAISE EXCEPTION 'an actor and a reason are required' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_d FROM public.tenant_deletions d WHERE d.id = p_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'deletion not found' USING ERRCODE = 'TB004';
    END IF;
    IF v_d.state IN {TERMINAL} THEN
        RAISE EXCEPTION 'deletion is terminal' USING ERRCODE = 'TB004';
    END IF;
    IF v_d.holder IS NOT NULL AND v_d.expires_at > pg_catalog.clock_timestamp() THEN
        RAISE EXCEPTION 'deletion lease is held' USING ERRCODE = 'TB003';
    END IF;
    IF v_d.state = 'database' AND NOT EXISTS (
        SELECT 1 FROM public.tenants t WHERE t.id = v_d.tenant_id
    ) THEN
        RAISE EXCEPTION 'the tenant row is gone: the deletion can only finish'
            USING ERRCODE = 'TB004';
    END IF;
    UPDATE public.tenant_deletions d
       SET state = 'failed',
           last_error = pg_catalog.left('operator ' || p_actor || ': ' || p_reason, 1000),
           retry_at = NULL, {_FREE}, updated_at = now()
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
"""  # noqa: E501 -- the 9c2e5a7f1d84 body, verbatim


def _replaced(body: str, old: str, new: str) -> str:
    """`body` with `old` replaced by `new`; anything but exactly one match is an error."""
    if body.count(old) != 1:
        raise ValueError(f"expected text not found exactly once in the function body: {old[:60]!r}")
    return body.replace(old, new)


_DELETE_EDITS = (
    (
        "CREATE FUNCTION tb_delete_tenant(p_id uuid) RETURNS void",
        "CREATE FUNCTION tb_delete_tenant(p_id uuid, p_holder text, p_fence bigint) RETURNS void",
    ),
    ("SET search_path = pg_catalog, public\n", "SET search_path = pg_catalog, public, pg_temp\n"),
    (
        "    v_not_before timestamptz;\n",
        "    v_not_before timestamptz;\n    v_holder text;\n    v_fence bigint;\n"
        "    v_expires timestamptz;\n    v_has_deletion boolean;\n",
    ),
    (
        "    -- the row lock: tb_upsert_tenant takes the advisory lock and then touches the row, "
        "so no\n    -- order can deadlock.\n",
        "    -- the deletion row and then the tenant row (the lock order of every function taking "
        "more\n    -- than one lock): tb_upsert_tenant takes the advisory lock and then touches "
        "the tenant\n    -- row, so no order can deadlock.\n",
    ),
    (
        "    SELECT * INTO v_row FROM public.tenants WHERE id = p_id FOR UPDATE;\n",
        """    SELECT d.not_before, d.holder, d.fence, d.expires_at
      INTO v_not_before, v_holder, v_fence, v_expires FROM public.tenant_deletions d
     WHERE d.tenant_id = p_id AND d.state = 'database' FOR UPDATE;
    v_has_deletion := FOUND;
    SELECT * INTO v_row FROM public.tenants WHERE id = p_id FOR UPDATE;
""",
    ),
    (
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
    -- (holder, fence and an unexpired `expires_at`, by the wall clock).
    IF NOT v_has_deletion THEN
        RAISE EXCEPTION 'tenant has no deletion in its database step' USING ERRCODE = '55000';
    END IF;
    IF p_holder IS NULL OR p_fence IS NULL OR v_holder IS DISTINCT FROM p_holder
       OR v_fence IS DISTINCT FROM p_fence OR v_expires IS NULL
       OR v_expires <= pg_catalog.clock_timestamp() THEN
        RAISE EXCEPTION 'deletion lease lost' USING ERRCODE = 'TB003';
    END IF;
""",
    ),
)
_DELETE_FN = _PREVIOUS_DELETE_FN
for _old, _new in _DELETE_EDITS:
    _DELETE_FN = _replaced(_DELETE_FN, _old, _new)


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
_REFUSE_LIVE_DELETIONS = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM tenant_deletions "  # noqa: S608 -- constants only
    f"WHERE state NOT IN {TERMINAL}) THEN "
    "RAISE EXCEPTION 'non-terminal tenant deletions exist: let them finish or cancel them "
    "before downgrading' USING ERRCODE = '55000'; END IF; END $$"
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
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    # Catalog-only (constant defaults): the backoff state of a step.
    op.execute(
        "ALTER TABLE tenant_deletions ADD COLUMN IF NOT EXISTS attempts bigint NOT NULL DEFAULT 0"
    )
    op.execute("ALTER TABLE tenant_deletions ADD COLUMN IF NOT EXISTS retry_at timestamptz")
    op.execute(_ASSERT_PREVIOUS)
    # No release calls tb_delete_tenant (the engine does not exist yet): the signature may change.
    op.execute(f"-- squawk-ignore ban-drop-function\nDROP FUNCTION {OLD_DELETE}")
    for statement in (
        _CLAIM_FN,
        _RENEW_FN,
        _ADVANCE_FN,
        _RETRY_FN,
        _RELEASE_FN,
        _FAIL_FN,
        _OPERATOR_FAIL_FN,
        _CANCEL_FN,
        _FINISH_FN,
        _DELETE_FN,
    ):
        op.execute(statement)
    _grant_functions(NEW_FUNCTIONS)


def downgrade() -> None:
    """Downgrade schema: the 9c2e5a7f1d84 `tb_delete_tenant(uuid)` comes back."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    # Closes the window in which a request could commit between the guard and the drops.
    op.execute("LOCK TABLE tenant_deletions IN SHARE ROW EXCLUSIVE MODE")
    op.execute(_REFUSE_LIVE_DELETIONS)
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
