"""The remaining v1 read routes over fakes: guards, envelopes, deliveries, adapters, an event."""

import json
from typing import Any
from uuid import UUID, uuid4

import pytest

from tests.trustbridge.admin.helpers import ACME, OTHER, build, problem, when
from trustbridge.adapters.interface import Capabilities, WebhookAuth
from trustbridge.adapters.loader import load_adapter_classes
from trustbridge.admin import queries, routes
from trustbridge.admin.adapters import AdapterCatalog, capabilities_out, describe
from trustbridge.admin.schemas import (
    CredentialOut,
    DeliveryDetailOut,
    DeliveryOut,
    OperationOut,
    RepositoryOut,
    ResourceOut,
)
from trustbridge.audit import ActorType, AuditRecord, Outcome
from trustbridge.worker.secrets import MemorySecrets, SecretUnavailable

VIEWER = "trustbridge:viewer"
OPERATOR = "trustbridge:operator"
ACME_VIEWER = "trustbridge:github.com:acme:viewer"
ADMIN = "trustbridge:admin"
CANARY = "canary-connection-value-9f3a"


def _samples() -> dict[str, Any]:
    now = when()
    return {
        "repositories": RepositoryOut(tenant_id=ACME.id, repository_id=1, full_name="o/r"),
        "resources": ResourceOut(
            tenant_id=ACME.id, adapter="ghcr", resource_key="k", state={"name": "n"}, updated_at=now
        ),
        "credentials": CredentialOut(
            tenant_id=ACME.id,
            adapter="ghcr",
            scope_key="s",
            scope={},
            kind=None,
            state={},
            outputs=[],
            updated_at=now,
        ),
        "operations": OperationOut(
            id=uuid4(),
            tenant_id=ACME.id,
            adapter="ghcr",
            scope_key="s",
            kind="issue",
            status="pending",
            oidc_offered=False,
            base_state=None,
            base_kind=None,
            attempts=0,
            error=None,
            manual_action=None,
            created_at=now,
            updated_at=now,
        ),
    }


TENANT_LISTS = ("repositories", "resources", "credentials", "operations")


class Spy:
    """A stand-in for one `queries` function: records the call, serves one item and a cursor."""

    def __init__(self, item: object) -> None:
        """Serve `item`."""
        self.item = item
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def __call__(self, _session: object, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        """Record and answer."""
        self.calls.append((args, kwargs))
        return [self.item], "next-cursor"


@pytest.fixture
def spies(monkeypatch: pytest.MonkeyPatch) -> dict[str, Spy]:
    """Every tenant list read replaced by a spy."""
    found = {name: Spy(item) for name, item in _samples().items()}
    for name, spy in found.items():
        monkeypatch.setattr(queries, name, spy)
    return found


@pytest.mark.parametrize("name", TENANT_LISTS)
def test_a_tenant_list_needs_viewer_on_that_tenant_and_pages(
    name: str, spies: dict[str, Spy]
) -> None:
    """Inv. 1/5/7/10: the guard, the 404 for another tenant, the envelope, limit and cursor."""
    env = build()
    mine = env.client(ACME_VIEWER)
    body = mine.get(f"/admin/v1/tenants/{ACME.id}/{name}", params={"limit": 7, "cursor": "c"})
    assert body.status_code == 200, body.text
    assert set(body.json()) == {"items", "next_cursor"}
    assert body.json()["next_cursor"] == "next-cursor"
    assert len(body.json()["items"]) == 1
    assert spies[name].calls == [((ACME.id,), {"limit": 7, "cursor": "c"})]
    problem(mine.get(f"/admin/v1/tenants/{OTHER.id}/{name}"), 404, "not-found")
    problem(env.client().get(f"/admin/v1/tenants/{ACME.id}/{name}"), 404, "not-found")
    problem(mine.get(f"/admin/v1/tenants/{ACME.id}/{name}", params={"limit": 201}), 422)
    assert env.client(VIEWER).get(f"/admin/v1/tenants/{OTHER.id}/{name}").status_code == 200
    assert len(spies[name].calls) == 2  # the 404s and the 422 never reached the read


@pytest.mark.parametrize("name", TENANT_LISTS)
def test_a_tenant_list_without_a_database_is_503(name: str, spies: dict[str, Spy]) -> None:
    """Mock mode: the tenant resolves (fake access) but there is nothing to read."""
    del spies
    problem(
        build(factory=None).client(ACME_VIEWER).get(f"/admin/v1/tenants/{ACME.id}/{name}"),
        503,
        "unavailable",
    )


def _delivery(**extra: Any) -> DeliveryDetailOut:  # noqa: ANN401
    return DeliveryDetailOut(
        host="github.com",
        delivery_id="d1",
        event="push",
        status="dead",
        attempts=3,
        received_at=when(),
        next_attempt_at=None,
        last_relayed_at=None,
        processed_at=None,
        dead_at=when(),
        last_error="boom",
        body_sha256="h",
        has_body=True,
        **extra,
    )


def test_deliveries_are_an_instance_viewer_list_without_headers_or_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The list answers the envelope; a tenant grant is 403 (instance data)."""
    spy = Spy(DeliveryOut(**_delivery().model_dump(exclude={"payload"})))

    async def deliveries(session: object, *, limit: int, cursor: str | None) -> Any:  # noqa: ANN401
        return await spy(session, limit=limit, cursor=cursor)

    monkeypatch.setattr(queries, "deliveries", deliveries)
    env = build()
    page = env.client(VIEWER).get("/admin/v1/deliveries", params={"limit": 3}).json()
    assert page["next_cursor"] == "next-cursor"
    assert set(page["items"][0]) >= {"host", "delivery_id", "status", "attempts", "last_error"}
    assert not {"headers", "body", "payload"} & set(page["items"][0])
    assert spy.calls == [((), {"limit": 3, "cursor": None})]
    problem(env.client(ACME_VIEWER).get("/admin/v1/deliveries"), 403, "forbidden")
    problem(build(factory=None).client(VIEWER).get("/admin/v1/deliveries"), 503, "unavailable")


def test_one_delivery_gives_the_payload_to_an_operator_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Viewer: no payload is requested. Operator and admin: it is. Headers never appear."""
    asked: list[bool] = []

    async def delivery(
        _session: object, host: str, delivery_id: str, *, with_payload: bool
    ) -> DeliveryDetailOut | None:
        asked.append(with_payload)
        if (host, delivery_id) != ("github.com", "d1"):
            return None
        return _delivery(payload={"action": "opened"} if with_payload else None)

    monkeypatch.setattr(queries, "delivery", delivery)
    env = build()
    viewer = env.client(VIEWER).get("/admin/v1/deliveries/github.com/d1")
    assert viewer.status_code == 200
    assert viewer.json()["payload"] is None
    operator = env.client(OPERATOR).get("/admin/v1/deliveries/github.com/d1").json()
    assert operator["payload"] == {"action": "opened"}
    assert "headers" not in operator
    assert env.client(ADMIN).get("/admin/v1/deliveries/github.com/d1").json()["payload"]
    assert asked == [False, True, True]
    problem(env.client(OPERATOR).get("/admin/v1/deliveries/github.com/nope"), 404, "not-found")
    problem(env.client(ACME_VIEWER).get("/admin/v1/deliveries/github.com/d1"), 403, "forbidden")
    problem(build(factory=None).client(OPERATOR).get("/admin/v1/deliveries/a/b"), 503)


# ---- GET /audit-events/{id} -----------------------------------------------------------------


def _record(event_id: UUID) -> AuditRecord:
    return AuditRecord(
        id=event_id,
        seq=9,
        occurred_at=when(),
        recorded_at=when(),
        tenant_id=None,
        actor_type=ActorType.SYSTEM,
        actor_sub="worker",
        actor_iss=None,
        on_behalf_of=None,
        source_ip=None,
        action="tenant.sync",
        resource_type="tenant",
        resource_id=None,
        outcome=Outcome.SUCCESS,
        before=None,
        after=None,
        request_id=None,
        trace_id=None,
        schema_version=1,
    )


def test_one_audit_event_is_an_instance_auditor_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auditor only (not admin, not a tenant auditor); unknown id 404; `export` is not an id."""
    known = uuid4()

    async def get_event(_session: object, event_id: UUID) -> AuditRecord | None:
        return _record(event_id) if event_id == known else None

    monkeypatch.setattr(routes, "get_event", get_event)

    async def listed(*_args: object, **_kwargs: object) -> Any:  # noqa: ANN401
        from trustbridge.audit import AuditPage  # noqa: PLC0415

        return AuditPage([], None)

    monkeypatch.setattr(routes, "list_events", listed)
    env = build()
    auditor = env.client("trustbridge:auditor")
    body = auditor.get(f"/admin/v1/audit-events/{known}").json()
    assert body["id"] == str(known)
    assert body["tenant_id"] is None
    problem(auditor.get(f"/admin/v1/audit-events/{uuid4()}"), 404, "not-found")
    problem(auditor.get("/admin/v1/audit-events/not-a-uuid"), 422, "validation")
    problem(env.client(ADMIN).get(f"/admin/v1/audit-events/{known}"), 403, "forbidden")
    problem(
        env.client("trustbridge:github.com:acme:auditor").get(f"/admin/v1/audit-events/{known}"),
        403,
        "forbidden",
    )
    problem(
        build(factory=None).client("trustbridge:auditor").get(f"/admin/v1/audit-events/{known}"),
        503,
    )


# ---- GET /tenants/{t}/adapters (inv. 8) ---------------------------------------------------------


def _catalog(secrets: MemorySecrets, *names: str) -> AdapterCatalog:
    return AdapterCatalog(load_adapter_classes(names), secrets)


def test_the_adapters_say_configured_and_capabilities_and_never_a_value() -> None:
    """Inv. 8: a planted canary connection value appears in no response and is never read."""
    secrets = MemorySecrets(
        {
            f"store:tenants/{ACME.id}/dockerhub": json.dumps(
                {"namespace": CANARY, "base_url": f"https://{CANARY}.example"}
            ),
            f"store:tenants/{OTHER.id}/jfrog": json.dumps({"base_url": CANARY}),
            f"store:tenants/{ACME.id}/not-an-adapter": CANARY,
        }
    )
    env = build(factory=None)  # the adapters route reads no table
    object.__setattr__(env.runtime, "adapters", _catalog(secrets, "dockerhub", "ghcr", "jfrog"))
    client = env.client(ACME_VIEWER)
    response = client.get(f"/admin/v1/tenants/{ACME.id}/adapters")
    assert response.status_code == 200, response.text
    items = {i["name"]: i for i in response.json()["items"]}
    assert list(items) == ["dockerhub", "ghcr", "jfrog"]
    assert [items[n]["configured"] for n in items] == [True, False, False]
    assert items["dockerhub"]["capabilities"]["needs_privileged_credential"] is True
    assert items["ghcr"]["capabilities"]["github_hosts"] == ["ghe.com", "ghes", "github.com"]
    assert secrets.reads == 0
    # Every admin GET, as an all-powerful caller, never shows the canary.
    everything = env.client(ADMIN, "trustbridge:auditor")
    for path in (
        f"/admin/v1/tenants/{ACME.id}/adapters",
        f"/admin/v1/tenants/{OTHER.id}/adapters",
        "/admin/v1/me",
        "/admin/v1/openapi.json",
    ):
        assert CANARY not in everything.get(path).text, path
    assert secrets.reads == 0


def test_adapters_page_and_empty_catalog() -> None:
    """Pagination over the names; no catalog means no adapters, not an error."""
    secrets = MemorySecrets()
    env = build()
    object.__setattr__(env.runtime, "adapters", _catalog(secrets, "argocd", "ghcr", "harbor"))
    client = env.client(ACME_VIEWER)
    first = client.get(f"/admin/v1/tenants/{ACME.id}/adapters", params={"limit": 2}).json()
    assert [i["name"] for i in first["items"]] == ["argocd", "ghcr"]
    second = client.get(
        f"/admin/v1/tenants/{ACME.id}/adapters", params={"cursor": first["next_cursor"]}
    ).json()
    assert [i["name"] for i in second["items"]] == ["harbor"]
    assert second["next_cursor"] is None
    object.__setattr__(env.runtime, "adapters", None)
    assert client.get(f"/admin/v1/tenants/{ACME.id}/adapters").json() == {
        "items": [],
        "next_cursor": None,
    }
    problem(client.get(f"/admin/v1/tenants/{OTHER.id}/adapters"), 404, "not-found")


def test_an_unavailable_connection_store_is_503() -> None:
    """A store that cannot be listed is not "nothing configured"."""
    secrets = MemorySecrets()
    secrets.down = False

    class Down:
        async def list_refs(self, prefix: str) -> list[str]:
            raise SecretUnavailable(prefix)

    env = build()
    object.__setattr__(
        env.runtime, "adapters", AdapterCatalog(load_adapter_classes(["ghcr"]), Down())
    )
    problem(
        env.client(ACME_VIEWER).get(f"/admin/v1/tenants/{ACME.id}/adapters"), 503, "unavailable"
    )


def test_every_built_in_adapter_describes_itself() -> None:
    """`describe` works without an instance for all five entry points, and validates (A7)."""
    classes = load_adapter_classes(["jfrog", "argocd", "harbor", "ghcr", "dockerhub"])
    for name, cls in classes.items():
        caps = capabilities_out(describe(cls))
        assert caps.rotation in {"none", "single", "two_phase"}, name
    webhook = capabilities_out(describe(classes["argocd"])).webhook
    assert webhook is not None
    assert webhook.auth == WebhookAuth.STATIC_HEADER.value
    assert capabilities_out(describe(classes["ghcr"])).webhook is None


def test_an_adapter_that_cannot_describe_itself_still_lists() -> None:
    """The failure is logged and `capabilities` is null; the name stays."""

    class Broken:
        name = "broken"
        api_version = 1

        def capabilities(self) -> Capabilities:
            raise RuntimeError("needs state")

    secrets = MemorySecrets({f"store:tenants/{ACME.id}/broken": "{}"})
    catalog = AdapterCatalog({"broken": Broken}, secrets)  # type: ignore[dict-item]
    env = build()
    object.__setattr__(env.runtime, "adapters", catalog)
    body = env.client(ACME_VIEWER).get(f"/admin/v1/tenants/{ACME.id}/adapters").json()
    assert body["items"] == [{"name": "broken", "configured": True, "capabilities": None}]
