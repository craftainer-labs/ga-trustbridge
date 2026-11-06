"""NG6e's guard additions: the instance-only tenant guard, the body bound and the mutation walk.

No mutating route exists yet, so the walk over the real router passes vacuously
(`test_routes_unit.py`); here fixture routes prove it fails on a mutation without an emitter.
"""

import dataclasses
from typing import Any

import httpx
import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from tests.trustbridge.admin.helpers import ACME, OTHER, Env, build, problem, token
from tests.trustbridge.helpers import make_settings
from trustbridge.admin import guards
from trustbridge.admin.guards import BoundedRoute, emits, guard, mutations_without_emitter
from trustbridge.audit import Action
from trustbridge.rbac import Role

ADMIN = "trustbridge:admin"
OPERATOR = "trustbridge:operator"
ACME_ADMIN = "trustbridge:github.com:acme:admin"
OTHER_ADMIN = "trustbridge:github.com:other:admin"
LIMIT = 64


def _env(**settings: Any) -> Env:  # noqa: ANN401 -- settings overrides
    """The admin app plus fixture mutating routes, with a 64-byte body bound."""
    env = build(**settings)
    extra = APIRouter(prefix="/admin/v1", route_class=BoundedRoute)

    @extra.post(
        "/fixture/tenants/{tenant_id}/disable",
        dependencies=guard(Role.ADMIN, "mutation", tenant_param="tenant_id", instance_only=True),
    )
    @emits(Action.TENANT_DISABLE)
    async def disable(tenant_id: str) -> dict[str, str]:
        return {"tenant": tenant_id}

    @extra.post("/fixture/echo", dependencies=guard(Role.OPERATOR, "mutation"))
    @emits(Action.DELIVERY_REQUEUE)
    async def echo(body: dict[str, Any]) -> dict[str, Any]:
        return body

    env.app.include_router(extra)
    env.app.state.admin = dataclasses.replace(env.runtime, max_body_bytes=LIMIT)
    return env


def test_an_instance_admin_reaches_a_tenant_route() -> None:
    """Instance-scope `admin` passes and the handler runs."""
    env = _env()
    response = env.client(ADMIN).post(f"/admin/v1/fixture/tenants/{ACME.id}/disable")
    assert response.status_code == 200, response.text
    assert response.json() == {"tenant": str(ACME.id)}
    assert env.sink.denials == []


def test_a_tenant_scoped_admin_is_rejected_with_403_and_a_role_denial() -> None:
    """Disable is instance-scoped: an `admin` on the tenant itself is not enough."""
    env = _env()
    response = env.client(ACME_ADMIN).post(f"/admin/v1/fixture/tenants/{ACME.id}/disable")
    problem(response, 403, "forbidden")
    (denial,) = env.sink.denials
    assert (denial.reason, denial.tenant_id, denial.scope) == (
        "role_missing",
        str(ACME.id),
        "instance",
    )


def test_a_weaker_instance_role_is_rejected_with_403() -> None:
    """An instance `operator` is below `admin`."""
    env = _env()
    problem(env.client(OPERATOR).post(f"/admin/v1/fixture/tenants/{ACME.id}/disable"), 403)


def test_a_tenant_without_any_grant_is_the_same_404_as_an_unknown_one() -> None:
    """The ids stay unenumerable; both are `tenant_not_granted` denials with the requested id."""
    env = _env()
    ungranted = env.client(OTHER_ADMIN).post(f"/admin/v1/fixture/tenants/{ACME.id}/disable")
    unknown = env.client(ADMIN).post(
        "/admin/v1/fixture/tenants/00000000-0000-4000-8000-000000000001/disable"
    )
    first = problem(ungranted, 404, "not-found")
    second = problem(unknown, 404, "not-found")
    assert first["detail"] == second["detail"]
    assert first["title"] == second["title"]
    assert [d.reason for d in env.sink.denials] == ["tenant_not_granted"] * 2
    assert env.sink.denials[0].tenant_id == str(ACME.id)
    assert OTHER.id != ACME.id


def test_a_non_canonical_tenant_spelling_is_a_404() -> None:
    """One spelling per tenant, as in `require_role`."""
    env = _env()
    spelled = str(ACME.id).upper()
    problem(env.client(ADMIN).post(f"/admin/v1/fixture/tenants/{spelled}/disable"), 404)
    problem(env.client(ADMIN).post("/admin/v1/fixture/tenants/not-a-uuid/disable"), 404)


def test_instance_only_needs_a_tenant_parameter() -> None:
    """The mode is meaningless without a tenant route."""
    with pytest.raises(ValueError, match="tenant_param"):
        guard(Role.ADMIN, "mutation", instance_only=True)


def test_a_body_within_the_bound_is_read() -> None:
    """Under the bound the body reaches the handler."""
    env = _env()
    response = env.client(OPERATOR).post("/admin/v1/fixture/echo", json={"a": 1})
    assert response.json() == {"a": 1}


def test_a_content_length_over_the_bound_is_a_413_before_the_body_is_read() -> None:
    """413 with the `payload-too-large` class, whatever the caller's token says."""
    env = _env()
    big = {"a": "x" * 200}
    problem(env.client(OPERATOR).post("/admin/v1/fixture/echo", json=big), 413, "payload-too-large")
    anonymous = TestClient(env.app)
    problem(anonymous.post("/admin/v1/fixture/echo", json=big), 413, "payload-too-large")


def test_a_chunked_body_over_the_bound_is_a_413() -> None:
    """No `Content-Length`: the stream is cut off when it grows past the bound."""
    env = _env()

    def chunks() -> Any:  # noqa: ANN401 -- a sync byte iterator
        for _ in range(10):
            yield b'{"a": "' + b"x" * 20 + b'"}'

    client = env.client(OPERATOR)
    response = client.post(
        "/admin/v1/fixture/echo", content=chunks(), headers={"content-type": "application/json"}
    )
    problem(response, 413, "payload-too-large")


async def test_a_malformed_content_length_is_a_400() -> None:
    """A length that is not a plain number is refused, not guessed."""
    env = _env()
    transport = httpx.ASGITransport(app=env.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://admin.test") as client:
        response = await client.post(
            "/admin/v1/fixture/echo",
            content=b"{}",
            headers={**token("u1", OPERATOR), "content-length": "12abc"},
        )
    problem(response, 400, "bad-request")


def test_the_mutation_walk_fails_for_a_route_without_an_emitter_or_the_mutation_bucket() -> None:
    """The walk is proven by fixtures: only a marked route on the mutation bucket passes."""
    router = APIRouter(prefix="/admin/v1", route_class=BoundedRoute)

    @router.post("/ok", dependencies=guard(Role.OPERATOR, "mutation"))
    @emits(Action.DELIVERY_REQUEUE)
    async def ok() -> None:
        return None

    @router.post("/no-emitter", dependencies=guard(Role.OPERATOR, "mutation"))
    async def no_emitter() -> None:
        return None

    @router.delete("/read-bucket", dependencies=guard(Role.OPERATOR))
    @emits(Action.TENANT_DISABLE)
    async def read_bucket() -> None:
        return None

    @router.get("/read", dependencies=guard(Role.VIEWER))
    async def read() -> None:
        return None

    assert mutations_without_emitter(router.routes) == [
        "POST /admin/v1/no-emitter",
        "DELETE /admin/v1/read-bucket",
    ]
    assert guards.EMITTERS[ok] is Action.DELIVERY_REQUEUE
    assert no_emitter not in guards.EMITTERS
    assert read_bucket in guards.EMITTERS
    assert read not in guards.EMITTERS


def test_mutations_count_against_their_own_bucket() -> None:
    """Decision 5: 60/minute by default, counted apart from reads (here 2 for the test)."""
    env = _env(admin_rate_limit_mutation_requests=2)
    client = env.client(OPERATOR)
    assert [client.post("/admin/v1/fixture/echo", json={}).status_code for _ in range(2)] == [
        200,
        200,
    ]
    limited = client.post("/admin/v1/fixture/echo", json={})
    problem(limited, 429, "rate-limited")
    assert int(limited.headers["retry-after"]) >= 1
    assert client.get("/admin/v1/hosts").status_code == 200  # the read bucket is untouched


def test_the_body_bound_setting_defaults_to_16_kib_and_must_be_positive() -> None:
    """`ADMIN_MAX_BODY_BYTES`."""
    assert make_settings().admin_max_body_bytes == 16384
    with pytest.raises(ValueError, match="admin_max_body_bytes"):
        make_settings(admin_max_body_bytes=0)
