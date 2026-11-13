"""refuse a tenant with a deletion in progress in tb_upsert_tenant (NG6h-2, ADR 0034)

Revision ID: 3b8d6f2a4c17
Revises: 9c2e5a7f1d84
Create Date: 2026-11-13 18:00:00.000000

Defence in depth for the tenant deletion engine (NG6h-4a): ``tb_upsert_tenant`` already returns
``NULL`` for a tombstoned key (``9c2e5a7f1d84``). It now also returns ``NULL``, and writes
nothing, while the tenant has a non-terminal ``tenant_deletions`` row (``requested``,
``draining``, ``platform``, ``github`` or ``database``), the same condition as the ``deleting``
flag of ``tb_get_tenant`` and the ``deleting`` reason of ``TenantDeletedError``. Without it an
installation event that passed an application check just before the deletion was requested could
still rewrite the tenant row (``org``, ``org_id``, ``state``) while its data is being removed.
The request function and this one take the same per-installation advisory lock before they
look, so the two serialise.

The lookup goes through ``tenants`` (its unique ``(host, installation_id)`` index) to the
deletion's partial unique index on ``tenant_id``, so a hot upsert reads one or two index entries.

``tb_set_tenant_disabled`` is unchanged: a deletion needs a disabled tenant, ``enable`` is already
refused while one is open (``9c2e5a7f1d84``), and a repeated ``disable`` keeps the first actor,
time and reason, so it cannot alter a deleting tenant.

Expand-only (NFR-0027): the same signature and return type, a body replaced. No release creates a
deletion before the engine (NG6h-4a) exists, so the new outcome is not reachable by the previous
release. The upgrade fails loudly if the ``9c2e5a7f1d84`` body is not the one it patches; the
downgrade restores it. ``schema_compat`` is not touched.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3b8d6f2a4c17"
down_revision: str | Sequence[str] | None = "9c2e5a7f1d84"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "trustbridge_app"
OWNER_ROLE = "trustbridge_owner"
SIGNATURE = "tb_upsert_tenant(text, bigint, text, bigint, text)"

# The body of 9c2e5a7f1d84, restored by the downgrade.
_PREVIOUS_UPSERT_FN = """
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
"""

_ANCHOR = """        RETURN NULL;
    END IF;
"""
_DELETING_GUARD = """    -- A deletion is in progress (any non-terminal state): leave the row alone.
    -- Same NULL outcome; the application ports tell the two apart (`deleting` or `tombstone`).
    IF EXISTS (
        SELECT 1 FROM public.tenants t
          JOIN public.tenant_deletions d ON d.tenant_id = t.id
           AND d.state NOT IN ('done', 'failed', 'cancelled')
         WHERE t.host = p_host AND t.installation_id = p_installation_id
    ) THEN
        RETURN NULL;
    END IF;
"""


def _replaced(body: str, old: str, new: str) -> str:
    """`body` with `old` replaced by `new`; a no-match is an error, never a silent no-op."""
    if body.count(old) != 1:
        raise ValueError(f"expected text not found exactly once in the function body: {old[:60]!r}")
    return body.replace(old, new)


_UPSERT_FN = _replaced(_PREVIOUS_UPSERT_FN, _ANCHOR, _ANCHOR + _DELETING_GUARD)


def _if_role(role: str, body: str) -> str:
    """A DO block running `body` only if `role` exists."""
    return (
        f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "  # noqa: S608 -- constants only
        f"{body} END IF; END $$"
    )


def _replace_function(statement: str) -> None:
    """Replace the body and re-apply the grants (idempotent: same signature, same owner)."""
    op.execute("SET lock_timeout = '5s'")
    op.execute("SET statement_timeout = '120s'")
    op.execute(statement)
    op.execute(f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(_if_role(APP_ROLE, f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO {APP_ROLE};"))
    op.execute(_if_role(OWNER_ROLE, f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO {OWNER_ROLE};"))
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")


def upgrade() -> None:
    """Upgrade schema."""
    _replace_function(_UPSERT_FN)


def downgrade() -> None:
    """Downgrade schema."""
    _replace_function(_PREVIOUS_UPSERT_FN)
