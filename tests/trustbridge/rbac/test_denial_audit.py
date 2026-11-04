"""The dependency hands every denial of an authenticated principal to the audit sink.

ADR 0033 invariants 3 and 5, ADR 0032 invariant 10: an `authz.denied` carries the `sub`, the
missing role or the requested tenant id, and nothing is recorded for a request that is allowed or
unauthenticated. The sink's own event shape is in `tests/trustbridge/audit/test_emit_unit.py`.
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.oidc import get_current_claims
from app.problem_details import register_problem_handlers
from trustbridge.rbac import (
    Authorized,
    Denial,
    RbacConfig,
    RbacRuntime,
    Role,
    TenantRef,
    require_role,
)

CLIENT = "api"
ACME = TenantRef(uuid4(), "github.com", "acme")
OTHER = TenantRef(uuid4(), "github.com", "other")


class Access:
    """A `TenantAccess` over fixed tenants."""

    def __init__(self, tenants: Sequence[TenantRef]) -> None:
        """Hold the tenants."""
        self.tenants = tenants

    async def get_tenant(self, tenant_id: UUID) -> TenantRef | None:
        """Look the tenant up."""
        return next((t for t in self.tenants if t.id == tenant_id), None)

    @contextmanager
    def tenant_context(self, tenant_id: UUID) -> Iterator[None]:
        """No context is needed here."""
        del tenant_id
        yield


class Sink:
    """A `DenialSink` that records, or fails when told to."""

    def __init__(self, *, fail: bool = False) -> None:
        """Choose whether `denied` raises."""
        self.denials: list[Denial] = []
        self.fail = fail

    async def denied(self, denial: Denial) -> None:
        """Record the denial (or fail like an unwritable audit table)."""
        if self.fail:
            raise RuntimeError("audit table unwritable")
        self.denials.append(denial)


def _app(sink: Sink | None) -> FastAPI:
    app = FastAPI()
    register_problem_handlers(app)
    app.state.rbac = RbacRuntime(RbacConfig(CLIENT), Access([ACME, OTHER]), sink)

    @app.get("/inst/admin")
    async def inst_admin(
        auth: Annotated[Authorized, Depends(require_role(Role.ADMIN))],
    ) -> dict[str, Any]:
        return {"sub": auth.principal.sub}

    @app.get("/t/{tenant_id}/operator")
    async def t_operator(
        tenant_id: str,
        auth: Annotated[Authorized, Depends(require_role(Role.OPERATOR, tenant_param="tenant_id"))],
    ) -> dict[str, Any]:
        return {"tenant": str(auth.tenant_id)}

    return app


def _client(
    app: FastAPI,
    *roles: str,
    iss: str | None = "https://idp.example",
    client: tuple[str, int] = ("203.0.113.9", 5000),
    raise_errors: bool = True,
) -> TestClient:
    claims: dict[str, Any] = {"sub": "u1", "resource_access": {CLIENT: {"roles": list(roles)}}}
    if iss is not None:
        claims["iss"] = iss
    app.dependency_overrides[get_current_claims] = lambda: claims
    return TestClient(app, client=client, raise_server_exceptions=raise_errors)


def test_an_instance_role_denial_is_recorded_with_the_sub_the_role_and_the_address() -> None:
    """403 on an instance route: reason role_missing, the required role, no tenant."""
    sink = Sink()
    response = _client(_app(sink), "trustbridge:viewer").get(
        "/inst/admin", headers={"X-Request-ID": "req-42"}
    )
    assert response.status_code == 403
    (denial,) = sink.denials
    assert denial == Denial(
        sub="u1",
        iss="https://idp.example",
        reason="role_missing",
        required_role="admin",
        scope="instance",
        tenant_id=None,
        source_ip="203.0.113.9",
        request_id="req-42",
    )


def test_an_ungranted_tenant_is_a_404_and_records_the_requested_id() -> None:
    """Inv. 5: the 404 for a tenant the principal has no grant on is still a denial row."""
    sink = Sink()
    client = _client(_app(sink), "trustbridge:github.com:other:admin")
    response = client.get(f"/t/{ACME.id}/operator")
    assert response.status_code == 404
    (denial,) = sink.denials
    assert (denial.reason, denial.scope, denial.tenant_id) == (
        "tenant_not_granted",
        "tenant",
        str(ACME.id),
    )
    assert denial.required_role == "operator"


def test_an_unknown_tenant_records_the_id_but_a_malformed_one_records_none() -> None:
    """A well-formed unknown id is recorded; free text from the path is not."""
    sink = Sink()
    client = _client(_app(sink), "trustbridge:admin")
    missing = uuid4()
    assert client.get(f"/t/{missing}/operator").status_code == 404
    assert client.get("/t/%7Bnot-a-uuid%7D/operator").status_code == 404
    assert [d.tenant_id for d in sink.denials] == [str(missing), None]


def test_a_too_weak_tenant_grant_is_a_403_with_the_tenant_id() -> None:
    """A grant on the tenant that lacks the role: role_missing, naming the tenant."""
    sink = Sink()
    client = _client(_app(sink), "trustbridge:github.com:acme:viewer")
    assert client.get(f"/t/{ACME.id}/operator").status_code == 403
    (denial,) = sink.denials
    assert (denial.reason, denial.scope, denial.tenant_id) == (
        "role_missing",
        "tenant",
        str(ACME.id),
    )


def test_an_allowed_request_records_nothing() -> None:
    """Only denials are events here."""
    sink = Sink()
    client = _client(_app(sink), "trustbridge:admin")
    assert client.get("/inst/admin").status_code == 200
    assert client.get(f"/t/{ACME.id}/operator").status_code == 200
    assert sink.denials == []


def test_an_unauthenticated_request_records_nothing() -> None:
    """User decision 1: a rejected token is a log and a metric, never a row."""
    sink = Sink()
    assert TestClient(_app(sink)).get("/inst/admin").status_code == 401
    assert sink.denials == []


def test_a_malformed_address_or_request_id_is_dropped_not_stored() -> None:
    """A scoped IPv6 address and a request id with odd characters become None."""
    sink = Sink()
    client = _client(_app(sink), "trustbridge:viewer", client=("fe80::1%eth0", 1))
    client.get("/inst/admin", headers={"X-Request-ID": "bad id with spaces"})
    client = _client(_app(sink), "trustbridge:viewer", client=("not-an-ip", 1))
    client.get("/inst/admin")
    assert [(d.source_ip, d.request_id) for d in sink.denials] == [(None, None), (None, None)]


def test_a_sink_failure_fails_the_request_instead_of_answering_normally() -> None:
    """Fail closed: if the denial cannot be written the caller gets an error, not a quiet 403."""
    client = _client(_app(Sink(fail=True)), "trustbridge:viewer", raise_errors=False)
    assert client.get("/inst/admin").status_code == 500


def test_without_a_sink_the_dependency_behaves_as_before() -> None:
    """The sink is optional until NG6d wires it."""
    assert _client(_app(None), "trustbridge:viewer").get("/inst/admin").status_code == 403
