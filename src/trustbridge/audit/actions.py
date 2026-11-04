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
    # Deferred: the admin API (NG6d, NG6e), host registration, deletion (NG6h).
    AUDIT_EXPORT = "audit.export"
    TENANT_DISABLE = "tenant.disable"
    CREDENTIAL_ROTATE = "credential.rotate"
    DELIVERY_REQUEUE = "delivery.requeue"
    HOST_REGISTRATION_START = "host.registration.start"


class SystemActor(StrEnum):
    """The fixed names a `system` actor may carry (it has no `actor_iss`)."""

    WORKER = "worker"
    RECONCILER = "reconciler"
    GITHUB_WEBHOOK = "github-webhook"
    MIGRATE = "migrate"
