"""add the append-only audit log (NG6c, ADR 0032)

Revision ID: e5c9a37d2b18
Revises: d4b8f1e60a27
Create Date: 2026-11-04 09:00:00.000000

Expand migration for the previous release, which never touches the new table. It creates
``audit_events`` and, from the database outward, the three append-only layers of ADR 0032:

* **grants**: ``trustbridge_app`` gets ``SELECT`` and ``INSERT`` and nothing else;
* **triggers**: ``BEFORE UPDATE OR DELETE`` (row) and ``BEFORE TRUNCATE`` (statement) triggers
  raise unless ``current_user`` is ``trustbridge_owner``; a ``BEFORE INSERT`` trigger stamps
  ``occurred_at`` (``now()``) and ``recorded_at`` (``clock_timestamp()``) so a caller's values
  are ignored;
* **one purge path**: ``tb_audit_purge(before, min_days, batch)``, ``SECURITY DEFINER`` with a
  fixed ``search_path``, which refuses a cut-off inside the retention floor and writes its own
  summary event. ``tb_audit_list(...)`` is the instance-wide (cross-tenant) read path.

Row-level security is enabled and forced on this table from creation (ADR 0031 "audit_events is
a seventh RLS table": no previous release touches it, so it needs no staged rollout). It is
enabled only when both roles exist, like every grant and policy here, so a dev database without
the roles keeps working; production refuses a missing role in the previous revision.

Roles are created outside migrations (the DBA or the Helm chart); this revision only ``GRANT``s.
Function ownership is not changed here: the functions are owned by whoever runs the migration
(the owner role in production, ADR 0028); moving them to ``trustbridge_owner`` explicitly is
deferred to release N+1 for all definer functions (the NG6a review finding).

``schema_compat`` is written with ``GREATEST`` and no bump. Lock-safe: a new table, so its
indexes need no ``CONCURRENTLY``; lock and statement timeouts are set first.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5c9a37d2b18"
down_revision: str | Sequence[str] | None = "d4b8f1e60a27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_COMPAT_VERSION = 1
APP_ROLE = "trustbridge_app"
OWNER_ROLE = "trustbridge_owner"
FUNCTIONS = (
    "tb_audit_purge(timestamptz, integer, integer)",
    "tb_audit_list(uuid, bigint, bigint, timestamptz, timestamptz, text, text, text, text, text,"
    " double precision, boolean, integer)",
)


def _if_roles(roles: Sequence[str], body: str) -> str:
    """A DO block running `body` only if every role in `roles` exists."""
    cond = " AND ".join(f"EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{r}')" for r in roles)  # noqa: S608 -- constants only
    return f"DO $$ BEGIN IF {cond} THEN {body} END IF; END $$"


_CREATE_TABLE = """
CREATE TABLE audit_events (
    id uuid NOT NULL,
    seq bigint GENERATED ALWAYS AS IDENTITY,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    tenant_id uuid,
    actor_type text NOT NULL,
    actor_sub text NOT NULL,
    actor_iss text,
    on_behalf_of text,
    source_ip inet,
    action text NOT NULL,
    resource_type text NOT NULL,
    resource_id text,
    outcome text NOT NULL,
    "before" jsonb,
    "after" jsonb,
    request_id text,
    trace_id text,
    schema_version bigint NOT NULL DEFAULT 1,
    CONSTRAINT pk_audit_events PRIMARY KEY (id),
    CONSTRAINT uq_audit_events_seq UNIQUE (seq),
    CONSTRAINT ck_audit_events_actor CHECK (
        (actor_type = 'user' AND actor_iss IS NOT NULL)
        OR (actor_type = 'system' AND actor_iss IS NULL)
    ),
    CONSTRAINT ck_audit_events_outcome CHECK (outcome IN ('success', 'denied', 'failure'))
)
"""

_GUARD_FN = """
CREATE OR REPLACE FUNCTION tb_audit_guard() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
    IF current_user <> 'trustbridge_owner' THEN
        RAISE EXCEPTION 'audit_events is append-only' USING ERRCODE = '42501';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NULL;
END
$$
"""

_STAMP_FN = """
CREATE OR REPLACE FUNCTION tb_audit_stamp() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
    NEW.occurred_at := now();
    NEW.recorded_at := clock_timestamp();
    RETURN NEW;
END
$$
"""

_PURGE_FN = """
CREATE OR REPLACE FUNCTION tb_audit_purge(
    p_before timestamptz, p_min_days integer, p_batch integer DEFAULT 1000
) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_deleted bigint;
BEGIN
    IF p_min_days IS NULL OR p_min_days < 1 THEN
        RAISE EXCEPTION 'invalid retention floor' USING ERRCODE = '22023';
    END IF;
    IF p_batch IS NULL OR p_batch < 1 OR p_batch > 10000 THEN
        RAISE EXCEPTION 'invalid batch size' USING ERRCODE = '22023';
    END IF;
    IF p_before IS NULL OR p_before > now() - make_interval(days => p_min_days) THEN
        RAISE EXCEPTION 'cut-off is inside the retention floor' USING ERRCODE = '22023';
    END IF;
    DELETE FROM public.audit_events a
    USING (
        SELECT e.id FROM public.audit_events e
        WHERE e.occurred_at < p_before ORDER BY e.seq LIMIT p_batch
    ) d
    WHERE a.id = d.id;
    GET DIAGNOSTICS v_deleted = ROW_COUNT;
    IF v_deleted > 0 THEN
        INSERT INTO public.audit_events (
            id, actor_type, actor_sub, action, resource_type, outcome, "after"
        ) VALUES (
            gen_random_uuid(), 'system', 'reconciler', 'audit.purge', 'audit_events', 'success',
            jsonb_build_object('before', p_before, 'deleted', v_deleted, 'min_days', p_min_days)
        );
    END IF;
    RETURN v_deleted;
END
$$
"""

_LIST_FN = """
CREATE OR REPLACE FUNCTION tb_audit_list(
    p_tenant_id uuid DEFAULT NULL,
    p_after_seq bigint DEFAULT NULL,
    p_before_seq bigint DEFAULT NULL,
    p_since timestamptz DEFAULT NULL,
    p_until timestamptz DEFAULT NULL,
    p_actor_sub text DEFAULT NULL,
    p_action text DEFAULT NULL,
    p_resource_type text DEFAULT NULL,
    p_resource_id text DEFAULT NULL,
    p_outcome text DEFAULT NULL,
    p_lag_seconds double precision DEFAULT NULL,
    p_descending boolean DEFAULT false,
    p_limit integer DEFAULT 100
) RETURNS SETOF public.audit_events
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_limit_seq bigint := 9223372036854775807;
BEGIN
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 1000 THEN
        RAISE EXCEPTION 'invalid page size' USING ERRCODE = '22023';
    END IF;
    IF p_lag_seconds IS NOT NULL THEN
        -- Stop before the first event still inside the lag, so a late commit is never skipped.
        SELECT COALESCE(min(r.seq), 9223372036854775807) INTO v_limit_seq
        FROM public.audit_events r
        WHERE r.recorded_at > clock_timestamp() - make_interval(secs => p_lag_seconds);
    END IF;
    IF p_descending THEN
        RETURN QUERY
        SELECT a.* FROM public.audit_events a
        WHERE a.seq < v_limit_seq
          AND (p_tenant_id IS NULL OR a.tenant_id = p_tenant_id)
          AND (p_after_seq IS NULL OR a.seq > p_after_seq)
          AND (p_before_seq IS NULL OR a.seq < p_before_seq)
          AND (p_since IS NULL OR a.occurred_at >= p_since)
          AND (p_until IS NULL OR a.occurred_at < p_until)
          AND (p_actor_sub IS NULL OR a.actor_sub = p_actor_sub)
          AND (p_action IS NULL OR a.action = p_action)
          AND (p_resource_type IS NULL OR a.resource_type = p_resource_type)
          AND (p_resource_id IS NULL OR a.resource_id = p_resource_id)
          AND (p_outcome IS NULL OR a.outcome = p_outcome)
        ORDER BY a.seq DESC LIMIT p_limit;
    ELSE
        RETURN QUERY
        SELECT a.* FROM public.audit_events a
        WHERE a.seq < v_limit_seq
          AND (p_tenant_id IS NULL OR a.tenant_id = p_tenant_id)
          AND (p_after_seq IS NULL OR a.seq > p_after_seq)
          AND (p_before_seq IS NULL OR a.seq < p_before_seq)
          AND (p_since IS NULL OR a.occurred_at >= p_since)
          AND (p_until IS NULL OR a.occurred_at < p_until)
          AND (p_actor_sub IS NULL OR a.actor_sub = p_actor_sub)
          AND (p_action IS NULL OR a.action = p_action)
          AND (p_resource_type IS NULL OR a.resource_type = p_resource_type)
          AND (p_resource_id IS NULL OR a.resource_id = p_resource_id)
          AND (p_outcome IS NULL OR a.outcome = p_outcome)
        ORDER BY a.seq ASC LIMIT p_limit;
    END IF;
END
$$
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute(_CREATE_TABLE)
    op.execute("CREATE INDEX ix_audit_events_tenant_seq ON audit_events (tenant_id, seq)")
    op.execute("CREATE INDEX ix_audit_events_occurred_at ON audit_events (occurred_at)")
    op.execute("CREATE INDEX ix_audit_events_actor_seq ON audit_events (actor_sub, seq)")
    # The export's lag bound looks up the first recent event by `recorded_at`.
    op.execute("CREATE INDEX ix_audit_events_recorded_at ON audit_events (recorded_at)")

    for statement in (_GUARD_FN, _STAMP_FN, _PURGE_FN, _LIST_FN):
        op.execute(statement)
    op.execute(
        "CREATE TRIGGER tb_audit_stamp BEFORE INSERT ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION tb_audit_stamp()"
    )
    op.execute(
        "CREATE TRIGGER tb_audit_guard_row BEFORE UPDATE OR DELETE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION tb_audit_guard()"
    )
    op.execute(
        "CREATE TRIGGER tb_audit_guard_truncate BEFORE TRUNCATE ON audit_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION tb_audit_guard()"
    )

    for signature in FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    grants = ", ".join(FUNCTIONS)
    op.execute(_if_roles([APP_ROLE], f"GRANT EXECUTE ON FUNCTION {grants} TO {APP_ROLE};"))
    op.execute(_if_roles([OWNER_ROLE], f"GRANT EXECUTE ON FUNCTION {grants} TO {OWNER_ROLE};"))
    op.execute(_if_roles([APP_ROLE], f"GRANT SELECT, INSERT ON audit_events TO {APP_ROLE};"))

    op.execute(
        _if_roles(
            [APP_ROLE],
            f"CREATE POLICY audit_insert ON audit_events FOR INSERT TO {APP_ROLE} "
            "WITH CHECK (tenant_id IS NULL OR tenant_id = (SELECT tb_current_tenant())); "
            f"CREATE POLICY audit_select ON audit_events FOR SELECT TO {APP_ROLE} "
            "USING (tenant_id = (SELECT tb_current_tenant()));",
        )
    )
    op.execute(
        _if_roles(
            [OWNER_ROLE],
            f"CREATE POLICY owner_all ON audit_events FOR ALL TO {OWNER_ROLE} "
            "USING (true) WITH CHECK (true);",
        )
    )
    # Enabled and forced from creation (ADR 0031), but only once both roles exist: forced RLS
    # without the owner policy would lock the table out for its own owner.
    op.execute(
        _if_roles(
            [APP_ROLE, OWNER_ROLE],
            "ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY; "
            "ALTER TABLE audit_events FORCE ROW LEVEL SECURITY;",
        )
    )

    op.execute(
        "INSERT INTO schema_compat (id, version) VALUES (1, "  # noqa: S608 -- constants and ints only
        f"{SCHEMA_COMPAT_VERSION}) ON CONFLICT (id) DO UPDATE SET version = "
        f"GREATEST(schema_compat.version, {SCHEMA_COMPAT_VERSION})"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    # The guard trigger refuses TRUNCATE/DELETE for non-owners; DROP TABLE is not blocked by it.
    op.execute("DROP TABLE IF EXISTS audit_events")
    for signature in (*reversed(FUNCTIONS), "tb_audit_stamp()", "tb_audit_guard()"):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
