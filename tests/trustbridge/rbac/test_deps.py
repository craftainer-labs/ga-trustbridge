"""The dependency over a throwaway app (ADR 0033 invariants 3-7, 6 ordering; 0031 invariant 5)."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Annotated, Any
from uuid import UUID, uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.oidc import get_current_claims
from app.problem_details import register_problem_handlers
from trustbridge.rbac import Authorized, RbacConfig, RbacRuntime, Role, TenantRef, require_role

CLIENT = "api"
ACME = TenantRef(uuid4(), "github.com", "acme")
OTHER = TenantRef(uuid4(), "github.com", "other")


class FakeAccess:
    """A `TenantAccess` recording the order of its calls."""

    def __init__(self, tenants: Sequence[TenantRef]) -> None:
        """Hold the tenants `list_tenants` returns."""
        self.tenants = tenants
        self.events: list[str] = []

    async def get_tenant(self, tenant_id: UUID) -> TenantRef | None:
        """Record the (closed-session) read."""
        self.events.append("get")
        return next((t for t in self.tenants if t.id == tenant_id), None)

    @contextmanager
    def tenant_context(self, tenant_id: UUID) -> Iterator[None]:
        """Record entering and leaving the context."""
        self.events.append(f"enter:{tenant_id}")
        try:
            yield
        finally:
            self.events.append("exit")


def _app(access: FakeAccess | None, *, config: RbacConfig | None = None) -> FastAPI:
    app = FastAPI()
    register_problem_handlers(app)
    if access is not None:
        app.state.rbac = RbacRuntime(config or RbacConfig(CLIENT), access)

    @app.get("/inst/admin")
    async def inst_admin(
        auth: Annotated[Authorized, Depends(require_role(Role.ADMIN))],
    ) -> dict[str, Any]:
        return {"sub": auth.principal.sub, "tenant": auth.tenant_id}

    @app.get("/inst/viewer")
    async def inst_viewer(
        auth: Annotated[Authorized, Depends(require_role(Role.VIEWER))],
    ) -> dict[str, Any]:
        return {"sub": auth.principal.sub}

    @app.get("/inst/auditor")
    async def inst_auditor(
        auth: Annotated[Authorized, Depends(require_role(Role.AUDITOR))],
    ) -> dict[str, Any]:
        return {"sub": auth.principal.sub}

    @app.get("/t/{tenant_id}/operator")
    async def t_operator(
        tenant_id: str,
        auth: Annotated[Authorized, Depends(require_role(Role.OPERATOR, tenant_param="tenant_id"))],
    ) -> dict[str, Any]:
        assert access is not None
        access.events.append("handler")
        return {"tenant": str(auth.tenant_id)}

    return app


def _client(
    app: FastAPI, *roles: str, sub: str | None = "u1", raise_errors: bool = True
) -> TestClient:
    """A client whose token is a MODE=mock-shaped claim set (same `resource_access` shape)."""
    claims: dict[str, Any] = {"resource_access": {CLIENT: {"roles": list(roles)}}}
    if sub is not None:
        claims["sub"] = sub
    app.dependency_overrides[get_current_claims] = lambda: claims
    return TestClient(app, raise_server_exceptions=raise_errors)


def _problem(response: Any, status: int) -> dict[str, Any]:  # noqa: ANN401
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert {"type", "title", "status", "detail", "instance"} <= body.keys()
    return body  # type: ignore[no-any-return]


def test_no_token_is_401_problem_with_the_challenge() -> None:
    """Inv. 2 (the template's authentication runs first): no token is 401 problem details."""
    response = TestClient(_app(FakeAccess([]))).get("/inst/viewer")
    _problem(response, 401)
    assert "resource_metadata" in response.headers["www-authenticate"]


def test_token_without_sub_is_401() -> None:
    """NFR-0014: a validated token with no `sub` is rejected as invalid."""
    client = _client(_app(FakeAccess([])), "trustbridge:admin", sub=None)
    response = client.get("/inst/admin")
    _problem(response, 401)
    assert 'error="invalid_token"' in response.headers["www-authenticate"]


def test_unwired_app_fails_loudly() -> None:
    """An app without `app.state.rbac` is a configuration error, not an open door."""
    client = _client(_app(None), "trustbridge:admin", raise_errors=False)
    assert client.get("/inst/admin").status_code == 500


@pytest.mark.parametrize(
    "roles", [[], ["trustbridge:viewer"], ["trustbridge:operator"], ["editor"]]
)
def test_instance_route_403_without_enough_role(roles: list[str]) -> None:
    """Inv. 3: no role, or not enough, is 403 problem details."""
    body = _problem(_client(_app(FakeAccess([])), *roles).get("/inst/admin"), 403)
    assert body["detail"] == "Insufficient role"


def test_instance_route_accepts_the_ladder_and_aliases() -> None:
    """The ladder holds through the dependency: admin reads as viewer; maintainer is admin."""
    app = _app(FakeAccess([]))
    assert _client(app, "trustbridge:admin").get("/inst/viewer").json() == {"sub": "u1"}
    assert _client(app, "maintainer").get("/inst/admin").status_code == 200
    assert _client(app, "trustbridge:auditor").get("/inst/viewer").status_code == 200


def test_audit_is_not_on_the_ladder() -> None:
    """Only auditor reads audit: admin and operator get 403, security (aliased) gets 200."""
    app = _app(FakeAccess([]))
    for role in ("trustbridge:admin", "trustbridge:operator", "maintainer", "editor"):
        assert _client(app, role).get("/inst/auditor").status_code == 403
    assert _client(app, "security").get("/inst/auditor").status_code == 200
    assert (
        _client(app, "trustbridge:admin", "trustbridge:auditor").get("/inst/auditor").status_code
        == 200
    )


def test_instance_route_rejects_a_tenant_scoped_grant() -> None:
    """Inv. 7: a tenant-scoped admin grant is not an instance grant."""
    client = _client(_app(FakeAccess([ACME])), "trustbridge:github.com:acme:admin")
    _problem(client.get("/inst/admin"), 403)


def test_persona_alias_off_makes_personas_inert() -> None:
    """Inv. 19a through the dependency: with the setting off a persona is a 403."""
    app = _app(FakeAccess([]), config=RbacConfig(CLIENT, alias_personas=False))
    assert _client(app, "maintainer").get("/inst/admin").status_code == 403
    assert _client(app, "trustbridge:admin").get("/inst/admin").status_code == 200


def test_separator_setting_reaches_the_parser() -> None:
    """A non-default separator is honoured end to end."""
    app = _app(FakeAccess([]), config=RbacConfig(CLIENT, separator="|"))
    assert _client(app, "trustbridge|admin").get("/inst/admin").status_code == 200
    assert _client(app, "trustbridge:admin").get("/inst/admin").status_code == 403


def test_tenant_route_runs_the_check_before_the_context() -> None:
    """Inv. 6 and 0031 inv. 5: list (closed session), then enter, then the handler, then exit."""
    access = FakeAccess([ACME, OTHER])
    client = _client(_app(access), "trustbridge:github.com:Acme:operator")
    response = client.get(f"/t/{ACME.id}/operator")
    assert response.json() == {"tenant": str(ACME.id)}
    assert access.events == ["get", f"enter:{ACME.id}", "handler", "exit"]


def test_instance_grant_covers_a_tenant_route() -> None:
    """An instance-scope role applies to every tenant."""
    client = _client(_app(FakeAccess([ACME])), "trustbridge:admin")
    assert client.get(f"/t/{ACME.id}/operator").status_code == 200


def test_tenant_grant_on_another_tenant_is_404_and_never_enters_the_context() -> None:
    """Inv. 5 and 7: no grant on that tenant is 404, identical to an unknown tenant."""
    access = FakeAccess([ACME, OTHER])
    client = _client(_app(access), "trustbridge:github.com:acme:admin")
    on_other = _problem(client.get(f"/t/{OTHER.id}/operator"), 404)
    unknown = _problem(client.get(f"/t/{uuid4()}/operator"), 404)
    malformed = _problem(client.get("/t/not-a-uuid/operator"), 404)
    for body in (unknown, malformed):
        assert {k: v for k, v in body.items() if k != "instance"} == {
            k: v for k, v in on_other.items() if k != "instance"
        }
    assert not any(e.startswith("enter") for e in access.events)


def test_principal_with_no_roles_gets_404_on_a_tenant_route() -> None:
    """Inv. 5: a token with no roles learns nothing about which tenants exist."""
    _problem(_client(_app(FakeAccess([ACME])), "junk").get(f"/t/{ACME.id}/operator"), 404)


def test_role_naming_an_org_with_no_tenant_grants_nothing() -> None:
    """Inv. 4: a role for an org that has no tenant row is a 404 everywhere."""
    client = _client(_app(FakeAccess([ACME])), "trustbridge:github.com:ghost:admin")
    _problem(client.get(f"/t/{ACME.id}/operator"), 404)


def test_renamed_org_stops_matching_through_the_dependency() -> None:
    """Inv. 20: the tenant row's org changed, so the old role name is a 404."""
    renamed = TenantRef(ACME.id, "github.com", "acme-new")
    client = _client(_app(FakeAccess([renamed])), "trustbridge:github.com:acme:admin")
    _problem(client.get(f"/t/{ACME.id}/operator"), 404)


def test_too_weak_tenant_grant_is_403_without_the_context() -> None:
    """A grant on the tenant that is not enough is 403 and the context is never entered."""
    access = FakeAccess([ACME])
    client = _client(_app(access), "trustbridge:github.com:acme:viewer")
    _problem(client.get(f"/t/{ACME.id}/operator"), 403)
    assert access.events == ["get"]


def test_context_exits_when_the_handler_raises() -> None:
    """The context manager is left on error too (0031 inv. 5)."""
    access = FakeAccess([ACME])
    app = _app(access)

    @app.get("/t/{tenant_id}/boom")
    async def boom(
        _: Annotated[Authorized, Depends(require_role(Role.VIEWER, tenant_param="tenant_id"))],
    ) -> None:
        raise ValueError("boom")

    client = _client(app, "trustbridge:viewer", raise_errors=False)
    assert client.get(f"/t/{ACME.id}/boom").status_code == 500
    assert access.events[-1] == "exit"


def test_non_canonical_tenant_id_spellings_are_404() -> None:
    """One URL spelling per tenant: hex without hyphens and `urn:uuid:` forms are not found."""
    client = _client(_app(FakeAccess([ACME])), "trustbridge:viewer")
    for raw in (ACME.id.hex, f"urn:uuid:{ACME.id}", f"{{{ACME.id}}}"):
        _problem(client.get(f"/t/{raw}/operator"), 404)
    assert client.get(f"/t/{str(ACME.id).upper()}/operator").status_code == 200


def test_not_found_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A 404 leaves a server-side trace (the 403 path already did)."""
    client = _client(_app(FakeAccess([ACME])), "junk")
    with caplog.at_level("WARNING", logger="trustbridge.rbac.deps"):
        _problem(client.get(f"/t/{ACME.id}/operator"), 404)
    assert "Tenant not found or not granted" in caplog.text


def test_missing_tenant_param_is_a_server_error_not_a_404() -> None:
    """A route naming a path parameter it does not have is a configuration bug."""
    app = _app(FakeAccess([ACME]))

    @app.get("/typo")
    async def typo(
        _: Annotated[Authorized, Depends(require_role(Role.VIEWER, tenant_param="nope"))],
    ) -> None:
        return None

    client = _client(app, "trustbridge:viewer", raise_errors=False)
    assert client.get("/typo").status_code == 500
