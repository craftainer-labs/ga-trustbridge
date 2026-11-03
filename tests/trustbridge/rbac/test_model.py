"""The role ladder and tenant grant matching (ADR 0033 matrix, invariants 7 and 20)."""

from uuid import uuid4

import pytest

from trustbridge.rbac import (
    Grant,
    Principal,
    Role,
    TenantRef,
    confers,
    instance_roles,
    tenant_roles,
)

A = TenantRef(uuid4(), "github.com", "Acme")
B = TenantRef(uuid4(), "github.com", "other")
NO_ORG = TenantRef(uuid4(), "github.com", None)


def _principal(*grants: Grant) -> Principal:
    return Principal("u1", None, frozenset(grants))


@pytest.mark.parametrize(
    ("held", "conferred"),
    [
        (Role.VIEWER, {Role.VIEWER}),
        (Role.OPERATOR, {Role.OPERATOR, Role.VIEWER}),
        (Role.ADMIN, {Role.ADMIN, Role.OPERATOR, Role.VIEWER}),
        (Role.AUDITOR, {Role.AUDITOR, Role.VIEWER}),
    ],
)
def test_ladder(held: Role, conferred: set[Role]) -> None:
    """Viewer < operator < admin; auditor includes viewer; only auditor confers auditor."""
    assert confers([held]) == conferred
    assert confers([]) == frozenset()


def test_instance_roles_ignore_tenant_grants() -> None:
    """Inv. 7: an instance-scoped check never counts a tenant-scoped grant."""
    p = _principal(Grant(Role.ADMIN, "github.com", "acme"), Grant(Role.VIEWER))
    assert instance_roles(p) == {Role.VIEWER}


def test_tenant_grant_matches_case_insensitively_and_only_its_tenant() -> None:
    """Inv. 7 and 20: a grant on A confers nothing on B; org and host match case-insensitively."""
    p = _principal(Grant(Role.OPERATOR, "github.com", "acme"))
    assert tenant_roles(p, A) == {Role.OPERATOR, Role.VIEWER}
    assert tenant_roles(p, TenantRef(uuid4(), "GITHUB.COM", "ACME")) == {Role.OPERATOR, Role.VIEWER}
    assert tenant_roles(p, B) == frozenset()
    assert tenant_roles(p, TenantRef(uuid4(), "ghe.example", "acme")) == frozenset()


def test_renamed_org_stops_matching_the_old_role_name() -> None:
    """Inv. 20: once `tenants.org` changes, the old role name grants nothing."""
    p = _principal(Grant(Role.ADMIN, "github.com", "acme"))
    assert tenant_roles(p, TenantRef(A.id, "github.com", "acme-renamed")) == frozenset()


def test_org_without_a_name_and_instance_grants() -> None:
    """A tenant with no org login matches no tenant grant; an instance grant covers every tenant."""
    p = _principal(Grant(Role.ADMIN, "github.com", "acme"), Grant(Role.AUDITOR))
    assert tenant_roles(p, NO_ORG) == {Role.AUDITOR, Role.VIEWER}
    assert tenant_roles(p, B) == {Role.AUDITOR, Role.VIEWER}


@pytest.mark.parametrize(
    "args",
    [
        (Role.ADMIN, None, "acme"),
        (Role.ADMIN, "github.com", None),
        ("admin", None, None),
    ],
)
def test_grant_fails_closed_on_half_scope_or_bad_role(args: tuple[object, ...]) -> None:
    """A half-scoped grant must never become instance-wide; the role must be a `Role`."""
    with pytest.raises(ValueError, match="grant|role"):
        Grant(*args)  # type: ignore[arg-type]
