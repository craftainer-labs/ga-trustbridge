"""add tb_purge_tenant_deletions: the retention purge of finished deletion records (NG6h-3c-2)

Revision ID: 7b2d4f8a1c36
Revises: 4e9a1c7b5d23
Create Date: 2026-11-13 22:00:00.000000

ADR 0034 user decision B4: ``tenant_deletions`` retention is configurable
(``DELETION_RETENTION_DAYS``, default 0 = keep for ever). A ``done`` record holds the requester's
``sub`` and ``iss``, so storage limitation (GDPR Art. 5(1)(e)) applies to it. The tombstone that
stops a still-installed App from re-creating the tenant (B2) moved to ``tenant_tombstones`` in
``4e9a1c7b5d23``, so the record can go without ending it.

``tb_purge_tenant_deletions(p_before, p_min_days, p_batch)`` (``SECURITY DEFINER``, owner role,
``search_path = pg_catalog, public, pg_temp``, ``EXECUTE`` revoked from ``PUBLIC`` and granted to
the app role, the one runtime role) deletes at most ``p_batch`` records and returns
``(purged, unprotected)``:

* only ``done`` and ``cancelled`` records with ``updated_at < p_before`` are eligible; open and
  ``failed`` records are never touched (an operator may still resume or cancel a ``failed`` one);
* a ``done`` record is deleted only when its ``tenant_tombstones`` row exists. An eligible ``done``
  record without one is skipped and counted in ``unprotected`` (it can only exist if the tombstone
  invariant broke); the duty logs it at WARNING. A ``cancelled`` record needs no tombstone: a
  cancelled deletion left the tenant standing, and a new request makes a new record. It holds the
  requester's identity, so it follows the same retention (storage limitation). The purge therefore
  never deletes a ``done`` record whose tombstone is missing, and it does not repair anything (it
  does not re-run the backfill);
* the arguments are validated like ``tb_audit_purge`` (``22023``): ``p_min_days >= 1``,
  ``p_batch`` 1..10000, and a cut-off no younger than ``p_min_days`` (the floor a typo cannot pass);
* rows are taken ``FOR UPDATE SKIP LOCKED``, oldest first, so two callers never wait on each other.

The owner role gets ``DELETE`` on ``tenant_deletions`` (the app role keeps ``SELECT`` only).

Expand-only (NFR-0027): a new function and a grant; no table, column or existing body changes.
**Upgrade order (the one-way door):** the previous release (N-1) still reads ``done`` records, so
the purge must not run while an N-1 pod may exist. The repo's schema-compat integer
(``schema_compat``, ``REQUIRED_SCHEMA_VERSION``) only stops *new* code from running on an *old*
schema; it cannot express "no N-1 pods", so the guard is the setting: ``DELETION_RETENTION_DAYS``
defaults to 0 (off). Roll this release out fully, then set a retention. Once ``done`` records are
purged, the downgrade of ``4e9a1c7b5d23`` refuses (its tombstones have no ``done`` record behind
them), so a purge is a point of no return for rolling back past that revision. This downgrade only
drops the function and the grant.

All timeouts are ``SET LOCAL`` (they end with the migration's transaction; nothing leaks into the
session).
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7b2d4f8a1c36"
down_revision: str | Sequence[str] | None = "4e9a1c7b5d23"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "trustbridge_app"
OWNER_ROLE = "trustbridge_owner"
SIGNATURE = "tb_purge_tenant_deletions(timestamptz, integer, integer)"

_FN = """
CREATE OR REPLACE FUNCTION tb_purge_tenant_deletions(
    p_before timestamptz, p_min_days integer, p_batch integer DEFAULT 1000
) RETURNS TABLE (purged bigint, unprotected bigint)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    v_purged bigint;
    v_unprotected bigint;
BEGIN
    IF p_min_days IS NULL OR p_min_days < 1 THEN
        RAISE EXCEPTION 'invalid retention floor' USING ERRCODE = '22023';
    END IF;
    IF p_batch IS NULL OR p_batch < 1 OR p_batch > 10000 THEN
        RAISE EXCEPTION 'invalid batch size' USING ERRCODE = '22023';
    END IF;
    IF p_before IS NULL
       OR p_before > pg_catalog.now() - pg_catalog.make_interval(days => p_min_days) THEN
        RAISE EXCEPTION 'cut-off is inside the retention floor' USING ERRCODE = '22023';
    END IF;
    -- A cancelled record, or a done one whose tombstone exists. SKIP LOCKED: a record another
    -- caller (or a deletion finishing) holds is theirs. The EXISTS is in the same statement.
    DELETE FROM public.tenant_deletions t
    USING (
        SELECT d.id FROM public.tenant_deletions d
         WHERE d.updated_at < p_before
           AND (d.state = 'cancelled'
                OR (d.state = 'done' AND EXISTS (
                    SELECT 1 FROM public.tenant_tombstones s
                     WHERE s.host = d.host AND s.installation_id = d.installation_id)))
         ORDER BY d.updated_at, d.id
         LIMIT p_batch
         FOR UPDATE OF d SKIP LOCKED
    ) doomed
    WHERE t.id = doomed.id;
    GET DIAGNOSTICS v_purged = ROW_COUNT;
    -- Eligible by age but without a tombstone: never deleted, reported.
    SELECT pg_catalog.count(*) INTO v_unprotected
      FROM public.tenant_deletions d
     WHERE d.state = 'done' AND d.updated_at < p_before
       AND NOT EXISTS (
           SELECT 1 FROM public.tenant_tombstones s
            WHERE s.host = d.host AND s.installation_id = d.installation_id);
    RETURN QUERY SELECT v_purged, v_unprotected;
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
    # The definer runs as the owner role, which needs DELETE; the app role keeps SELECT only.
    op.execute(_if_role(OWNER_ROLE, f"GRANT DELETE ON tenant_deletions TO {OWNER_ROLE};"))


def downgrade() -> None:
    """Downgrade schema: the function and the grant go; no row is touched."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute(_if_role(OWNER_ROLE, f"REVOKE DELETE ON tenant_deletions FROM {OWNER_ROLE};"))
    op.execute(f"DROP FUNCTION IF EXISTS {SIGNATURE}")
