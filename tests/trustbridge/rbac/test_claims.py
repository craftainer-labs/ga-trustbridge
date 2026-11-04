"""Claims to grants: table-driven over claim shapes (ADR 0033 invariants 4, 19a, 20)."""

from typing import Any

import pytest

from trustbridge.rbac import Grant, Role, build_principal, parse_grants

CLIENT = "api"


def _claims(*roles: Any, client: str = CLIENT) -> dict[str, Any]:  # noqa: ANN401
    return {"sub": "u1", "resource_access": {client: {"roles": list(roles)}}}


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        # inv. 4: no resource_access, or any malformed shape, is no roles and not an error
        ({}, set()),
        ({"resource_access": None}, set()),
        ({"resource_access": "x"}, set()),
        ({"resource_access": {}}, set()),
        ({"resource_access": {CLIENT: None}}, set()),
        ({"resource_access": {CLIENT: {}}}, set()),
        ({"resource_access": {CLIENT: {"roles": "admin"}}}, set()),
        ({"resource_access": {CLIENT: {"roles": None}}}, set()),
        # roles of another client, and realm roles, are not read
        (_claims("trustbridge:admin", client="other"), set()),
        ({"realm_access": {"roles": ["trustbridge:admin"]}}, set()),
        ({"roles": ["trustbridge:admin"]}, set()),
        # non-string entries are ignored, valid ones kept
        (_claims(1, None, ["x"], "trustbridge:viewer"), {Grant(Role.VIEWER)}),
        # instance roles
        (_claims("trustbridge:admin"), {Grant(Role.ADMIN)}),
        (_claims("trustbridge:operator"), {Grant(Role.OPERATOR)}),
        (_claims("trustbridge:auditor"), {Grant(Role.AUDITOR)}),
        (_claims("trustbridge:viewer"), {Grant(Role.VIEWER)}),
        # tenant roles: host and org lower-cased, role exact
        (
            _claims("trustbridge:GitHub.com:Acme:operator"),
            {Grant(Role.OPERATOR, "github.com", "acme")},
        ),
        # inv. 4: anything else grants nothing
        (_claims("admin"), set()),
        (_claims("trustbridge"), set()),
        (_claims("trustbridge:"), set()),
        (_claims("trustbridge:root"), set()),
        (_claims("trustbridge:Admin"), set()),
        (_claims("TRUSTBRIDGE:admin"), set()),
        (_claims("trustbridge:github.com:acme"), set()),
        (_claims("trustbridge:github.com:acme:root"), set()),
        (_claims("trustbridge:github.com:acme:Admin"), set()),
        (_claims("trustbridge:github.com::admin"), set()),
        (_claims("trustbridge::acme:admin"), set()),
        (_claims("trustbridge:github.com:acme:extra:admin"), set()),
        (_claims("other:admin"), set()),
        (_claims("owner"), set()),
        # inv. 19a: each persona grants exactly its aliased role at instance scope
        (_claims("viewer"), {Grant(Role.VIEWER)}),
        (_claims("security"), {Grant(Role.AUDITOR)}),
        (_claims("detective"), {Grant(Role.AUDITOR)}),
        (_claims("editor"), {Grant(Role.OPERATOR)}),
        (_claims("maintainer"), {Grant(Role.ADMIN)}),
        (_claims("Viewer", "MAINTAINER"), set()),
        # the union of namespaced roles and personas
        (
            _claims("editor", "trustbridge:github.com:acme:admin", "junk"),
            {Grant(Role.OPERATOR), Grant(Role.ADMIN, "github.com", "acme")},
        ),
    ],
)
def test_parse_grants(claims: dict[str, Any], expected: set[Grant]) -> None:
    """Each claim shape parses to exactly the expected grants."""
    assert parse_grants(claims, client_id=CLIENT) == expected


@pytest.mark.parametrize("persona", ["viewer", "security", "detective", "editor", "maintainer"])
def test_personas_grant_nothing_when_the_alias_is_off(persona: str) -> None:
    """Inv. 19a: with RBAC_ALIAS_TEMPLATE_PERSONAS off only namespaced roles count."""
    claims = _claims(persona, "trustbridge:viewer")
    grants = parse_grants(claims, client_id=CLIENT, alias_personas=False)
    assert grants == {Grant(Role.VIEWER)}


def test_a_custom_separator_replaces_the_colon() -> None:
    """The separator setting changes the format and nothing else."""
    claims = _claims("trustbridge|admin", "trustbridge|github.com|acme|viewer", "trustbridge:admin")
    assert parse_grants(claims, client_id=CLIENT, separator="|") == {
        Grant(Role.ADMIN),
        Grant(Role.VIEWER, "github.com", "acme"),
    }


@pytest.mark.parametrize("sub", [None, "", 5])
def test_a_missing_or_unusable_sub_is_no_principal(sub: Any) -> None:  # noqa: ANN401
    """NFR-0014: `sub` is the one required claim."""
    assert build_principal({"sub": sub}, client_id=CLIENT) is None
    assert build_principal({}, client_id=CLIENT) is None


def test_principal_carries_sub_iss_and_grants() -> None:
    """The actor is the durable `sub`; a non-string `iss` is dropped."""
    claims = {**_claims("trustbridge:admin"), "iss": "https://idp"}
    principal = build_principal(claims, client_id=CLIENT)
    assert principal is not None
    assert (principal.sub, principal.iss, principal.grants) == (
        "u1",
        "https://idp",
        frozenset({Grant(Role.ADMIN)}),
    )
    bad = build_principal({"sub": "u2", "iss": 3}, client_id=CLIENT)
    assert bad is not None
    assert (bad.iss, bad.grants) == (None, frozenset())
