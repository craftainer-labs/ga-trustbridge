"""add tenant_tombstones: the tombstone of a deleted installation, apart from its record (NG6h-3c-1)

Revision ID: 4e9a1c7b5d23
Revises: 8c4e1a7d3b52
Create Date: 2026-11-13 21:00:00.000000

ADR 0034 user decision B2: after a deletion completes, a still-installed App's events for
``(host, installation_id)`` are acknowledged and dropped and no tenant is re-created. Until now the
tombstone was a ``done`` ``tenant_deletions`` row, which user decision B4 (configurable retention of
the deletion records, NG6h-3c-2) would remove. This revision gives the tombstone its own table, so
the records can be purged without ending the tombstone.

* ``tenant_tombstones (host, installation_id) PRIMARY KEY, tenant_id, deleted_at``: host-scoped,
  outside RLS, no foreign key (the tenant row is gone). Ids only, no tenant content. The app role
  has ``SELECT`` only; the owner role (which the definers run as) has ``SELECT, INSERT``, so a row
  is written only by ``tb_finish_tenant_deletion``. There is no ``UPDATE`` or ``DELETE`` grant: the
  purge of NG6h-3c-2 does not remove tombstones (they are kept indefinitely, one small row per
  deleted installation).
* ``tb_finish_tenant_deletion`` writes the tombstone in the transaction that marks the deletion
  ``done`` (``ON CONFLICT DO NOTHING``, so a repeat is harmless).
* ``tb_upsert_tenant`` (and, in the application, ``SqlTenants`` and ``SqlTenantGate``) read the
  tombstone from the new table. The in-flight rule stays on ``tenant_deletions``: a
  ``database``-step deletion whose tenant row ``tb_delete_tenant`` already removed is also a
  tombstone, because the tombstone row is only written by the finish step.
  ``TB001`` and ``TB002`` are unchanged.
* The backfill inserts one row per ``(host, installation_id)`` with a ``done`` deletion (the latest
  one's ``tenant_id`` and ``updated_at``), ``ON CONFLICT DO NOTHING``, so it is idempotent and a
  re-run after a partial failure is harmless. ``tenant_deletions`` is locked ``SHARE ROW EXCLUSIVE``
  for it, so no deletion finishes between the backfill and the function replacement.

Expand-only (NFR-0027): a new table, and two function bodies replaced with the same signatures,
return types, owners and grants. The previous release (N-1) keeps working against the new schema:
its readers still look at the ``done`` rows, which this revision does not touch, and a deletion
finished by an N-1 pod goes through the new ``tb_finish_tenant_deletion``, which writes the
tombstone for it. **N-1 note for NG6h-3c-2:** while an N-1 pod may still run, its readers depend on
the ``done`` rows, so the purge of ``done`` rows must not run until no N-1 pod exists (one release
after this one is fully rolled out). A pod finishing a deletion between this revision's backfill
and the lock above is impossible (the lock), but the purge function re-runs the same backfill
for the rows it removes, as a second safeguard.

Both replaced bodies are checked before they are replaced: ``md5(prosrc)`` of the live function must
equal the md5 of the exact expected body (``5d7a2c8e1b94`` for the finish function, ``3b8d6f2a4c17``
for the upsert on upgrade; this revision's on downgrade), or the migration fails (``55000``) instead
of overwriting a drifted function. The downgrade refuses (``55000``) while a tombstone has no
matching ``done`` deletion (the tombstone would be lost), restores both bodies and drops the
table; with the ``done`` rows kept (they are, until NG6h-3c-2) nothing is lost.
"""

import hashlib
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4e9a1c7b5d23"
down_revision: str | Sequence[str] | None = "8c4e1a7d3b52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "trustbridge_app"
OWNER_ROLE = "trustbridge_owner"
FINISH = "tb_finish_tenant_deletion(uuid, text, bigint, jsonb, jsonb)"
UPSERT = "tb_upsert_tenant(text, bigint, text, bigint, text)"

_TABLE = """
CREATE TABLE IF NOT EXISTS tenant_tombstones (
    host text NOT NULL,
    installation_id bigint NOT NULL,
    tenant_id uuid NOT NULL,
    deleted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (host, installation_id)
)
"""

_BACKFILL = """
INSERT INTO tenant_tombstones (host, installation_id, tenant_id, deleted_at)
SELECT DISTINCT ON (d.host, d.installation_id) d.host, d.installation_id, d.tenant_id, d.updated_at
  FROM tenant_deletions d
 WHERE d.state = 'done'
 ORDER BY d.host, d.installation_id, d.updated_at DESC, d.id
ON CONFLICT (host, installation_id) DO NOTHING
"""

# The bodies of 5d7a2c8e1b94 and 3b8d6f2a4c17 (the latter with pg_temp, as 6f3b9d2e4a85 left it),
# restored by the downgrade and checked before the upgrade.
_PREVIOUS_FINISH_FN = """
CREATE OR REPLACE FUNCTION tb_finish_tenant_deletion(
    p_id uuid, p_holder text, p_fence bigint,
    p_counts jsonb DEFAULT NULL, p_skipped jsonb DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    v_d public.tenant_deletions%ROWTYPE;
BEGIN
    IF (p_counts IS NOT NULL AND pg_catalog.jsonb_typeof(p_counts) <> 'object')
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
    IF v_d.state <> 'database' THEN
        RAISE EXCEPTION 'only the database step can finish' USING ERRCODE = 'TB004';
    END IF;
    IF EXISTS (SELECT 1 FROM public.tenants t WHERE t.id = v_d.tenant_id) THEN
        RAISE EXCEPTION 'the tenant row still exists' USING ERRCODE = 'TB004';
    END IF;
    -- `done` is the tombstone of (host, installation_id) (tb_upsert_tenant, SqlTenants).
    UPDATE public.tenant_deletions d
       SET state = 'done', step_cursor = '{}'::jsonb, counts = COALESCE(p_counts, d.counts),
           skipped = COALESCE(p_skipped, d.skipped), retry_at = NULL, last_error = NULL,
           holder = NULL, expires_at = NULL, updated_at = now()
     WHERE d.id = p_id;
END
$$
"""

_PREVIOUS_UPSERT_BODY = """
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
    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
        'tb_tenant:' || p_host || ':' || p_installation_id::text, 0));
    -- Tombstone: once a deletion of this installation is `done` (or is in its `database` step and
    -- tb_delete_tenant has already removed the row), no tenant is created or revived for it.
    -- TB001 is raised (never NULL: the other paths always return an id).
    IF EXISTS (
        SELECT 1 FROM public.tenant_deletions d
         WHERE d.host = p_host AND d.installation_id = p_installation_id
           AND (d.state = 'done'
                OR (d.state = 'database' AND NOT EXISTS (
                    SELECT 1 FROM public.tenants t
                     WHERE t.host = p_host AND t.installation_id = p_installation_id)))
    ) THEN
        RAISE EXCEPTION 'tenant tombstoned' USING ERRCODE = 'TB001';
    END IF;
    -- A deletion is in progress (any non-terminal state): leave the row alone.
    IF EXISTS (
        SELECT 1 FROM public.tenants t
          JOIN public.tenant_deletions d ON d.tenant_id = t.id
           AND d.state NOT IN ('done', 'failed', 'cancelled')
         WHERE t.host = p_host AND t.installation_id = p_installation_id
    ) THEN
        RAISE EXCEPTION 'tenant deletion in progress' USING ERRCODE = 'TB002';
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
"""

_SEARCH_PATH = "SET search_path = pg_catalog, public\n"


def _replaced(body: str, old: str, new: str) -> str:
    """`body` with `old` replaced by `new`; a no-match is an error, never a silent no-op."""
    if body.count(old) != 1:
        raise ValueError(f"expected text not found exactly once in the function body: {old[:60]!r}")
    return body.replace(old, new)


def _spliced(body: str, start: str, end: str, new: str) -> str:
    """`body` with the text from `start` up to (not including) `end` replaced by `new`."""
    first = body.index(start)
    if body.count(start) != 1 or body.count(end) != 1:
        raise ValueError(f"expected anchors not found exactly once: {start[:40]!r}")
    return body[:first] + new + body[body.index(end) :]


_PREVIOUS_UPSERT_FN = _PREVIOUS_UPSERT_BODY  # the text of 3b8d6f2a4c17
# The live function carries pg_temp since 6f3b9d2e4a85 (ALTER FUNCTION); `prosrc` is the same.
_PINNED_PREVIOUS_UPSERT_FN = _replaced(
    _PREVIOUS_UPSERT_FN, _SEARCH_PATH, "SET search_path = pg_catalog, public, pg_temp\n"
)

_FINISH_TOMBSTONE_COMMENT = (
    "    -- `done` is the tombstone of (host, installation_id) (tb_upsert_tenant, SqlTenants).\n"
)
_FINISH_FN = _replaced(
    _PREVIOUS_FINISH_FN,
    _FINISH_TOMBSTONE_COMMENT,
    """    -- The tombstone of (host, installation_id), written in the transaction that
    -- marks the deletion `done` (tb_upsert_tenant, SqlTenants and the gate read it). A repeat
    -- (an earlier deletion of the key) is harmless: the first tombstone stays.
    INSERT INTO public.tenant_tombstones (host, installation_id, tenant_id)
    VALUES (v_d.host, v_d.installation_id, v_d.tenant_id)
    ON CONFLICT (host, installation_id) DO NOTHING;
""",
)

_UPSERT_FN = _spliced(
    _PINNED_PREVIOUS_UPSERT_FN,
    "    IF EXISTS (\n        SELECT 1 FROM public.tenant_deletions d\n",
    "        RAISE EXCEPTION 'tenant tombstoned'",
    """    IF EXISTS (
        SELECT 1 FROM public.tenant_tombstones s
         WHERE s.host = p_host AND s.installation_id = p_installation_id
    ) OR EXISTS (
        SELECT 1 FROM public.tenant_deletions d
         WHERE d.host = p_host AND d.installation_id = p_installation_id
           AND d.state = 'database' AND NOT EXISTS (
                SELECT 1 FROM public.tenants t
                 WHERE t.host = p_host AND t.installation_id = p_installation_id)
    ) THEN
""",
)
_UPSERT_FN = _replaced(
    _UPSERT_FN,
    "    -- Tombstone: once a deletion of this installation is `done` (or is in its `database` step"
    " and\n    -- tb_delete_tenant has already removed the row), no tenant is created or revived"
    " for it.\n",
    "    -- Tombstone: once a deletion of this installation has finished (tenant_tombstones), or\n"
    "    -- is in its `database` step and tb_delete_tenant has removed the row, no tenant is\n"
    "    -- created or revived for it.\n",
)


def _body_md5(function_sql: str) -> str:
    """The md5 of the text between the outer ``$$`` of `function_sql`, which is the `prosrc`."""
    body = function_sql[function_sql.index("$$") + 2 : function_sql.rindex("$$")]
    return hashlib.md5(body.encode(), usedforsecurity=False).hexdigest()


def _assert_body(signature: str, function_sql: str, source: str) -> str:
    """A DO block failing (55000) unless the live body is exactly `function_sql`'s body."""
    return (
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_proc WHERE "  # noqa: S608 -- constants only
        f"oid = to_regprocedure('{signature}') "
        f"AND pg_catalog.md5(prosrc) = '{_body_md5(function_sql)}') THEN "
        f"RAISE EXCEPTION '{signature} is not the {source} body' "
        "USING ERRCODE = '55000'; END IF; END $$"
    )


def _if_role(role: str, body: str) -> str:
    """A DO block running `body` only if `role` exists."""
    return (
        f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "  # noqa: S608 -- constants only
        f"{body} END IF; END $$"
    )


_ASSERT_PREVIOUS = (
    _assert_body(FINISH, _PREVIOUS_FINISH_FN, "5d7a2c8e1b94"),
    _assert_body(UPSERT, _PREVIOUS_UPSERT_FN, "3b8d6f2a4c17"),
)
_ASSERT_NEW = (
    _assert_body(FINISH, _FINISH_FN, "4e9a1c7b5d23"),
    _assert_body(UPSERT, _UPSERT_FN, "4e9a1c7b5d23"),
)
# A tombstone with no `done` deletion behind it would be lost with the table: refuse.
_REFUSE_UNBACKED = (
    "DO $$ BEGIN IF to_regclass('public.tenant_tombstones') IS NOT NULL AND EXISTS ("
    "SELECT 1 FROM tenant_tombstones s WHERE NOT EXISTS (SELECT 1 FROM tenant_deletions d "
    "WHERE d.host = s.host AND d.installation_id = s.installation_id AND d.state = 'done')) THEN "
    "RAISE EXCEPTION 'cannot downgrade: a tenant tombstone has no done deletion record' "
    "USING ERRCODE = '55000'; END IF; END $$"
)


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    # No deletion finishes between the backfill and the function replacement (both commit
    # together), so no `done` row is left without its tombstone.
    op.execute("LOCK TABLE tenant_deletions IN SHARE ROW EXCLUSIVE MODE")
    for statement in _ASSERT_PREVIOUS:
        op.execute(statement)
    op.execute(_TABLE)
    # The app role only reads; the only writer is the finish definer (the owner role).
    op.execute(_if_role(APP_ROLE, f"GRANT SELECT ON tenant_tombstones TO {APP_ROLE};"))
    op.execute(_if_role(OWNER_ROLE, f"GRANT SELECT, INSERT ON tenant_tombstones TO {OWNER_ROLE};"))
    op.execute(_BACKFILL)
    # Same signatures and return types, so the owner and the grants stay.
    op.execute(_FINISH_FN)
    op.execute(_UPSERT_FN)


def downgrade() -> None:
    """Downgrade schema: the previous bodies come back and the table goes."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    # Closes the window in which a deletion could finish between the guard and the restore.
    op.execute("LOCK TABLE tenant_deletions IN SHARE ROW EXCLUSIVE MODE")
    op.execute("LOCK TABLE tenant_tombstones IN ACCESS EXCLUSIVE MODE")
    op.execute(_REFUSE_UNBACKED)
    for statement in _ASSERT_NEW:
        op.execute(statement)
    op.execute(_PREVIOUS_FINISH_FN)
    op.execute(_PINNED_PREVIOUS_UPSERT_FN)
    op.execute("-- squawk-ignore ban-drop-table\nDROP TABLE IF EXISTS tenant_tombstones")
