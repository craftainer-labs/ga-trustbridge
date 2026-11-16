"""add tb_skip_tenant_deletion_step: the operator's audited skip of a stuck step (NG6h-7b)

Revision ID: 2c8e5b1f7d34
Revises: 9a4d6c2e8b17
Create Date: 2026-11-16 12:00:00.000000

ADR 0034 user decision 2 (G-api, 2026-11-03; shapes confirmed 2026-11-16): a deletion that cannot
finish its step stays there, retrying with backoff, until an instance-scoped ``admin`` acknowledges
it with ``POST /admin/v1/tenant-deletions/{id}/skip-step``. This is the definer behind that route.

``tb_skip_tenant_deletion_step(p_id, p_actor)`` (``SECURITY DEFINER``, owner role, ``search_path =
pg_catalog, public, pg_temp``, ``EXECUTE`` revoked from ``PUBLIC`` and granted to the app role, the
one runtime role) locks the record and returns ``(outcome, from_state, to_state)``. Anything but
``skipped`` changes nothing:

* ``not_found``: no such record;
* ``terminal``: the record is ``done``, ``failed`` or ``cancelled``;
* ``not_skippable``: the state is not ``platform``, ``github`` or ``database`` (``requested`` has
  run nothing, ``draining`` is the drain that makes the later steps safe);
* ``not_failed``: the step has not failed yet (``attempts`` is 0): it is running or waiting for its
  first pass, and a skip is for an acknowledged failure;
* ``lease_held``: a live lease is held, so an engine is working on it (the lease is judged by the
  wall clock, as in ``5d7a2c8e1b94``).

A skip appends one entry to ``skipped`` (``category`` ``step``, ``reason`` ``operator_skipped``,
the ``step``, ``acknowledged_by`` the actor and ``acknowledged_at``; no free text), resets
``attempts``, ``retry_at`` and ``last_error`` (the next step starts clean), clears the cursor and
bumps ``fence``, so a holder of the old lease can never commit. ``platform`` moves to ``github``
and ``github`` to ``database``. The ``database`` step has no later state (the record is finished
by ``tb_finish_tenant_deletion`` after ``tb_delete_tenant`` removed the ``tenants`` row), so
skipping it stays in ``database`` with the cursor ``{"operator_skipped": true}``: the engine does
not run the step's batches again and goes straight to removing the ``tenants`` row, which
``tb_delete_tenant`` still refuses while any tenant-keyed row is left (``55000``, a retry). A skip
never lets a deletion finish with rows left.

Expand-only (NFR-0027): a new function and a grant; no table, column or existing body changes, and
the previous release never calls it. The downgrade drops the function.

All timeouts are ``SET LOCAL`` (they end with the migration's transaction).
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2c8e5b1f7d34"
down_revision: str | Sequence[str] | None = "9a4d6c2e8b17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "trustbridge_app"
OWNER_ROLE = "trustbridge_owner"
SIGNATURE = "tb_skip_tenant_deletion_step(uuid, text)"

_FN = """
CREATE FUNCTION tb_skip_tenant_deletion_step(p_id uuid, p_actor text)
RETURNS TABLE (outcome text, from_state text, to_state text)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    v_d public.tenant_deletions%ROWTYPE;
    v_next text;
    v_cursor jsonb;
    v_entry jsonb;
BEGIN
    IF p_actor IS NULL OR length(p_actor) NOT BETWEEN 1 AND 255 THEN
        RAISE EXCEPTION 'an actor is required' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_d FROM public.tenant_deletions d WHERE d.id = p_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN QUERY SELECT 'not_found'::text, NULL::text, NULL::text;
        RETURN;
    END IF;
    IF v_d.state IN ('done', 'failed', 'cancelled') THEN
        RETURN QUERY SELECT 'terminal'::text, v_d.state, NULL::text;
        RETURN;
    END IF;
    IF v_d.state NOT IN ('platform', 'github', 'database') THEN
        RETURN QUERY SELECT 'not_skippable'::text, v_d.state, NULL::text;
        RETURN;
    END IF;
    IF v_d.attempts < 1 THEN
        RETURN QUERY SELECT 'not_failed'::text, v_d.state, NULL::text;
        RETURN;
    END IF;
    IF v_d.holder IS NOT NULL AND v_d.expires_at > pg_catalog.clock_timestamp() THEN
        RETURN QUERY SELECT 'lease_held'::text, v_d.state, NULL::text;
        RETURN;
    END IF;
    IF v_d.state = 'database' THEN
        v_next := 'database';
        v_cursor := '{"operator_skipped": true}'::jsonb;
    ELSE
        v_next := CASE v_d.state WHEN 'platform' THEN 'github' ELSE 'database' END;
        v_cursor := '{}'::jsonb;
    END IF;
    v_entry := pg_catalog.jsonb_build_object(
        'category', 'step',
        'reason', 'operator_skipped',
        'step', v_d.state,
        'acknowledged_by', p_actor,
        'acknowledged_at', pg_catalog.to_char(
            pg_catalog.now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
    );
    UPDATE public.tenant_deletions d
       SET state = v_next, step_cursor = v_cursor,
           skipped = d.skipped || pg_catalog.jsonb_build_array(v_entry),
           attempts = 0, retry_at = NULL, last_error = NULL,
           holder = NULL, expires_at = NULL, fence = d.fence + 1, updated_at = now()
     WHERE d.id = p_id;
    RETURN QUERY SELECT 'skipped'::text, v_d.state, v_next;
END
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
    op.execute(_FN)
    op.execute(f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(_if_role(APP_ROLE, f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO {APP_ROLE};"))
    op.execute(_if_role(OWNER_ROLE, f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO {OWNER_ROLE};"))


def downgrade() -> None:
    """Downgrade schema: the function goes; no row is touched."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute(f"DROP FUNCTION IF EXISTS {SIGNATURE}")
