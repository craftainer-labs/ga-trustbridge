"""Claims to grants: the one place that knows the token's role claim shape (ADR 0033 decision 3).

A pure function. Roles come only from `resource_access.<client_id>.roles` (Keycloak client
roles); a second source would replace `_role_names` without touching routes. Nothing but `sub`
is assumed present (NFR-0014): a malformed or missing claim yields no roles, never an error.
"""

from collections.abc import Mapping
from typing import Any

from trustbridge.rbac.model import Grant, Principal, Role

PREFIX = "trustbridge"

# Template persona -> trustbridge role, at instance scope only (ADR 0033 decision 2).
PERSONA_ALIASES: Mapping[str, Role] = {
    "viewer": Role.VIEWER,
    "security": Role.AUDITOR,
    "detective": Role.AUDITOR,
    "editor": Role.OPERATOR,
    "maintainer": Role.ADMIN,
}

_INSTANCE_PARTS = 2  # prefix, role
_TENANT_PARTS = 4  # prefix, host, org, role

_ROLES_BY_NAME = {role.value: role for role in Role}


def _role_names(claims: Mapping[str, Any], client_id: str) -> list[str]:
    """The string entries of `resource_access.<client_id>.roles`; anything malformed is empty."""
    access = claims.get("resource_access")
    client = access.get(client_id) if isinstance(access, Mapping) else None
    roles = client.get("roles") if isinstance(client, Mapping) else None
    if not isinstance(roles, list):
        return []
    return [name for name in roles if isinstance(name, str)]


def _parse_name(name: str, *, separator: str, alias_personas: bool) -> Grant | None:
    """One role name to a grant, or None when it grants nothing."""
    if alias_personas and name in PERSONA_ALIASES:
        return Grant(PERSONA_ALIASES[name])
    parts = name.split(separator)
    if parts[0] != PREFIX or len(parts) not in {_INSTANCE_PARTS, _TENANT_PARTS}:
        return None
    role = _ROLES_BY_NAME.get(parts[-1])
    if role is None:
        return None
    if len(parts) == _INSTANCE_PARTS:
        return Grant(role)
    host, org = parts[1].lower(), parts[2].lower()
    if not host or not org:
        return None
    return Grant(role, host, org)


def parse_grants(
    claims: Mapping[str, Any], *, client_id: str, separator: str = ":", alias_personas: bool = True
) -> frozenset[Grant]:
    """The grants a token's client roles carry.

    Instance scope is `trustbridge<sep><role>` or (when `alias_personas`) a bare template
    persona; tenant scope is `trustbridge<sep><host><sep><org><sep><role>`. Host and org are
    lower-cased, the role is exact, and every other name grants nothing.
    """
    grants = (
        _parse_name(name, separator=separator, alias_personas=alias_personas)
        for name in _role_names(claims, client_id)
    )
    return frozenset(grant for grant in grants if grant is not None)


def build_principal(
    claims: Mapping[str, Any], *, client_id: str, separator: str = ":", alias_personas: bool = True
) -> Principal | None:
    """The principal for validated claims, or None when the token has no usable `sub`."""
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        return None
    iss = claims.get("iss")
    return Principal(
        sub=sub,
        iss=iss if isinstance(iss, str) else None,
        grants=parse_grants(
            claims, client_id=client_id, separator=separator, alias_personas=alias_personas
        ),
    )
