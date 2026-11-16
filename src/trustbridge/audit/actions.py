"""The closed set of audit actions and system actor names (ADR 0032 "Event model")."""

from enum import StrEnum


class Action(StrEnum):
    """Every action an event may carry (dotted verbs). An unknown action is refused.

    "Emitted" members have a call site on main; "deferred" members are named by ADR 0032 or 0033
    and belong to the item that builds the flow (see `audit/README.md`).
    """

    # Emitted today.
    AUTHZ_DENIED = "authz.denied"
    TENANT_SYNC = "tenant.sync"
    CREDENTIAL_ISSUE = "credential.issue"
    CREDENTIAL_ROTATE_BEGIN = "credential.rotate.begin"
    CREDENTIAL_ROTATE_FINISH = "credential.rotate.finish"
    CREDENTIAL_REVOKE = "credential.revoke"
    CREDENTIAL_OPERATION_ABANDONED = "credential.operation.abandoned"
    CREDENTIAL_ORPHAN_DELETE = "credential.orphan.delete"
    OIDC_TRUST_CREATE = "oidc_trust.create"
    OIDC_TRUST_REMOVE = "oidc_trust.remove"
    # Written by the database's own purge function (migration e5c9a37d2b18).
    AUDIT_PURGE = "audit.purge"
    # The deletion engine's own events (ADR 0034, NG6h-4a-2): dotted, written by the reconciler.
    TENANT_DELETION_STEP = "tenant.deletion.step"
    TENANT_DELETION_SKIPPED = "tenant.deletion.skipped"
    TENANT_DELETION_COMPLETED = "tenant.deletion.completed"
    TENANT_DELETION_FAILED = "tenant.deletion.failed"
    # The admin routes' events (ADR 0034, NG6h-7b), written in the transaction of the change; the
    # user put them in the engine's dotted family (G-api, 2026-11-16). `.operator_failed` is the
    # operator's `fail`; the engine's own non-retryable failure is `.failed`.
    TENANT_DELETION_REQUESTED = "tenant.deletion.requested"
    TENANT_DELETION_CANCELLED = "tenant.deletion.cancelled"
    TENANT_DELETION_STEP_SKIPPED = "tenant.deletion.step_skipped"
    TENANT_DELETION_OPERATOR_FAILED = "tenant.deletion.operator_failed"
    # The reconciler's summary of one processed tenant reconcile request (NG5r-c); user decision,
    # 2026-11-16. Unlike `tenant.reconcile` (the admin's request), these are written by the duty.
    TENANT_RECONCILE_COMPLETED = "tenant.reconcile.completed"
    TENANT_RECONCILE_FAILED = "tenant.reconcile.failed"
    # Deferred: the admin API (NG6d, NG6e), host registration.
    AUDIT_EXPORT = "audit.export"
    TENANT_DISABLE = "tenant.disable"
    CREDENTIAL_ROTATE = "credential.rotate"
    DELIVERY_REQUEUE = "delivery.requeue"
    HOST_REGISTRATION_START = "host.registration.start"
    # Named for the NG6e routes (ADR 0033); user decision, 2026-11-06.
    TENANT_ENABLE = "tenant.enable"
    TENANT_RECONCILE = "tenant.reconcile"
    HOST_REGISTRATION_MANUAL = "host.registration.manual"
    HOST_REGISTRATION_COMPLETE = "host.registration.complete"


class SystemActor(StrEnum):
    """The fixed names a `system` actor may carry (it has no `actor_iss`)."""

    WORKER = "worker"
    RECONCILER = "reconciler"
    GITHUB_WEBHOOK = "github-webhook"
    API = "api"  # the api process running the credential core for an admin call (rotate)
    MIGRATE = "migrate"
