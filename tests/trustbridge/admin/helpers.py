"""A throwaway app around the real admin router, with fakes for tenancy, audit and Valkey."""

import json
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from types import TracebackType
from typing import Annotated, Any, Self
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from app.oidc import bearer_challenge, get_current_claims
from app.problem_details import register_problem_handlers
from tests.trustbridge.helpers import make_settings
from trustbridge.admin.problems import install_problem_types
from trustbridge.admin.routes import router
from trustbridge.admin.runtime import AdminRuntime, build_limits
from trustbridge.github.availability import HostState, HostStatus
from trustbridge.ratelimit import InMemoryWindowCounter
from trustbridge.rbac import Denial, RbacConfig, RbacRuntime, TenantRef
from trustbridge.settings import TrustbridgeSettings

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
    """A `DenialSink` that records."""

    def __init__(self) -> None:
        """Start empty."""
        self.denials: list[Denial] = []

    async def denied(self, denial: Denial) -> None:
        """Record the denial."""
        self.denials.append(denial)


class Hosts:
    """The answering replica's host view."""

    def __init__(self, names: Sequence[str] = ("github.com", "ghe.example")) -> None:
        """One ready host and one unavailable one."""
        self._names = list(names)

    def statuses(self) -> dict[str, HostStatus]:
        """Each host's status."""
        return {
            name: HostStatus(HostState.READY)
            if i == 0
            else HostStatus(HostState.UNAVAILABLE, "key check failed")
            for i, name in enumerate(self._names)
        }


class FakeSession:
    """An `AsyncSession` stand-in: only `async with` and `.begin()` are used (reads are patched)."""

    async def __aenter__(self) -> Self:
        """Open."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close."""

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        """A transaction."""
        yield


def fake_factory() -> Any:  # noqa: ANN401 -- stands in for async_sessionmaker
    """A session factory whose sessions do nothing."""
    return FakeSession


class Env:
    """The app, its runtime pieces and a way to call it as a token holder."""

    def __init__(self, app: FastAPI, sink: Sink, runtime: AdminRuntime) -> None:
        """Keep the pieces tests assert on."""
        self.app, self.sink, self.runtime = app, sink, runtime

    def client(self, *roles: str, sub: str = "u1", host: str = "testclient") -> TestClient:
        """A client carrying a token with `roles` (a `resource_access` client-role claim set)."""
        return TestClient(self.app, client=(host, 50000), headers=token(sub, *roles))


def token(sub: str, *roles: str) -> dict[str, str]:
    """The Authorization header our fake validator turns into claims."""
    claims = {
        "sub": sub,
        "iss": "https://idp.example",
        "resource_access": {CLIENT: {"roles": list(roles)}},
    }
    return {"Authorization": "Bearer " + json.dumps(claims)}


async def _claims(authorization: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    if not authorization:
        raise HTTPException(401, "Not authenticated", headers=bearer_challenge())
    try:
        value = json.loads(authorization.removeprefix("Bearer "))
    except ValueError:
        raise HTTPException(
            401, "Invalid or expired token", headers=bearer_challenge("invalid_token")
        ) from None
    return value  # type: ignore[no-any-return]


def build(  # noqa: PLR0913 - every collaborator is optional
    *,
    tenants: Sequence[TenantRef] = (ACME, OTHER),
    settings: TrustbridgeSettings | None = None,
    counter: Any = None,  # noqa: ANN401
    factory: Any = "fake",  # noqa: ANN401
    writer: Any = None,  # noqa: ANN401
    sink: Sink | None = None,
    access: Any = None,  # noqa: ANN401
    denials: Any = None,  # noqa: ANN401
    export_page_size: int = 2,
    **settings_overrides: Any,  # noqa: ANN401
) -> Env:
    """The admin router under the template's problem handlers, wired like `main` wires it."""
    app = FastAPI()
    register_problem_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_current_claims] = _claims
    chosen = sink or Sink()
    settings = settings or make_settings(admin_rate_limit_window_seconds=3600, **settings_overrides)
    app.state.rbac = RbacRuntime(
        RbacConfig(CLIENT), access or Access(tenants), denials=denials or chosen
    )
    runtime = AdminRuntime(
        limits=build_limits(settings, counter or InMemoryWindowCounter()),
        hosts=Hosts(),
        session_factory=fake_factory() if factory == "fake" else factory,
        writer=writer,
        export_lag_seconds=0.5,
        export_page_size=export_page_size,
    )
    app.state.admin = runtime

    @app.get("/other")
    async def other() -> None:
        raise HTTPException(418, "teapot")

    install_problem_types(app)
    return Env(app, chosen, runtime)


def problem(response: Any, status: int, kind: str | None = None) -> dict[str, Any]:  # noqa: ANN401
    """Assert a problem+json response and return its body."""
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert {"type", "title", "status", "detail", "instance"} <= body.keys()
    if kind is not None:
        assert body["type"] == f"tag:trustbridge,2026:problem/{kind}"
    return body  # type: ignore[no-any-return]


def when(year: int = 2026) -> datetime:
    """A fixed aware timestamp."""
    return datetime(year, 11, 4, 12, 0, tzinfo=UTC)


__all__ = ["ACME", "OTHER", "Access", "Env", "Sink", "build", "problem", "token", "when"]
