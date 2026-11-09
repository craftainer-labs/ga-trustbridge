"""Host registration through the admin API over fakes (ADR 0030, ADR 0033, NG6e-4)."""

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from tests.trustbridge.admin.helpers import FakeSession, build, problem
from tests.trustbridge.fakes import FakeRedis
from tests.trustbridge.github.conftest import KeySigner, ScriptedGitHub, make_host_config
from tests.trustbridge.helpers import as_redis, make_settings
from tests.trustbridge.signer.test_signers import make_key, pem_of
from trustbridge.admin import guards, registration, routes
from trustbridge.admin.registration import (
    MemoryNonceStore,
    RegistrationRuntime,
    ValkeyNonceStore,
    build_deps,
    parse_body,
)
from trustbridge.admin.schemas import ManualIn
from trustbridge.audit import Action, AuditEvent, AuditWriter, Outcome
from trustbridge.github.availability import HostState
from trustbridge.github.client import GitHubApi, GitHubRequester, MaskManifestCode
from trustbridge.github.hosts import GitHubHostConfig
from trustbridge.github.manifest import AppKind, ManifestFlow, ManifestFlowError, StartRequest
from trustbridge.main import _registration_runtime
from trustbridge.rbac import Role
from trustbridge.signer.file import FileSigner

OPERATOR = "trustbridge:operator"
ADMIN = "trustbridge:admin"
VIEWER = "trustbridge:viewer"
ACME_ADMIN = "trustbridge:github.com:acme:admin"
PEM = pem_of(make_key())
WEBHOOK_SECRET = "whsec-canary-91d2c7"  # noqa: S105 -- a planted canary
CODE = "c0de-canary-5521"
START = "/admin/v1/hosts/github.com/registration"
COMPLETE = f"{START}/complete"
MANUAL = f"{START}/manual"


class Secrets:
    """Records secret-store writes (a `SecretWriter`)."""

    def __init__(self) -> None:
        """Start empty."""
        self.items: dict[str, str] = {}
        self.fail = False
        self.delay = 0.0
        self.trace: list[str] | None = None

    async def write(self, ref: str, value: SecretStr) -> None:
        """Store a secret (after `delay`), or fail."""
        if self.trace is not None:
            self.trace.append("secret")
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise OSError("secret store down")
        self.items[ref] = value.get_secret_value()


class Writer(AuditWriter):
    """An `AuditWriter` that records instead of writing."""

    def __init__(self, world: World) -> None:
        """Record into `world.written`."""
        self.world = world

    async def record(self, event: AuditEvent) -> UUID:
        """Record the event, or fail."""
        if self.world.writer_error is not None:
            raise self.world.writer_error
        self.world.written.append(event)
        return event.id


class World:
    """A fake database, secret store, GitHub and signer for the registration routes."""

    def __init__(self, tmp_path: Path) -> None:
        """An unregistered github.com with a file signer."""
        self.gh = ScriptedGitHub()
        self.secrets = Secrets()
        self.signer_path = tmp_path / "app.pem"
        self.stored: str | None = None
        self.insert_wins = True
        self.lock_free = True
        self.trace: list[str] = []
        self.events: list[AuditEvent] = []
        self.written: list[AuditEvent] = []
        self.writer_error: Exception | None = None
        self.state = HostState.UNREGISTERED
        self.config = make_host_config(app_id=None)
        self.now = 1_800_000_000.0
        self.signers: dict[str, Any] = {"main": FileSigner(self.signer_path)}
        self.script_conversion()

    def script_conversion(self, code: str = CODE) -> None:
        """GitHub answers the code exchange."""
        self.gh.route(
            "POST",
            f"/app-manifests/{code}/conversions",
            201,
            {
                "id": 123,
                "slug": "trustbridge-github-com",
                "html_url": "https://github.com/apps/trustbridge-github-com",
                "pem": PEM,
                "webhook_secret": WEBHOOK_SECRET,
                "client_secret": "csec",
                "client_id": "Iv1.x",
            },
        )

    def runtime(self) -> RegistrationRuntime:
        """The registration flow over the fakes."""

        def factory(host: GitHubHostConfig) -> GitHubRequester:
            return GitHubApi(host, async_transport=self.gh.transport())

        deps = build_deps(
            signers=self.signers,
            secrets=self.secrets,
            nonces=MemoryNonceStore(),
            state_key=b"k" * 32,
            public_base_url="https://tb.example",
        )
        return RegistrationRuntime(
            hosts={self.config.name: self.config},
            deps=replace(deps, api_factory=factory, clock=lambda: self.now),
            states=lambda: {self.config.name: self.state},
        )

    def database(self) -> Any:  # noqa: ANN401
        """A session factory: `scalar` is the stored registration, `execute` the insert."""
        world = self

        class Session(FakeSession):
            locks = False

            async def __aexit__(self, *exc: object) -> None:
                if self.locks:
                    world.trace.append("unlock")

            async def scalar(self, statement: object) -> str | None:
                del statement
                return world.stored

            async def execute(self, statement: object, params: dict[str, Any] | None = None) -> Any:  # noqa: ANN401
                del params
                if "pg_try_advisory_xact_lock" in str(statement):
                    self.locks = True
                    world.trace.append("lock")
                    return SimpleNamespace(scalar=lambda: world.lock_free)
                return SimpleNamespace(first=lambda: ("github.com",) if world.insert_wins else None)

        return Session


def _client(
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    *roles: str,
    runtime: bool = True,
    writer: bool = True,
    server_errors: bool = True,
) -> TestClient:
    env = build(factory=world.database())
    env.app.state.admin = replace(
        env.runtime,
        writer=Writer(world) if writer else None,
        registration=world.runtime() if runtime else None,
    )

    async def append(session: object, event: AuditEvent) -> UUID:
        del session
        world.events.append(event)
        return event.id

    monkeypatch.setattr(registration, "append", append)
    client = env.client(*roles)
    if not server_errors:
        client = TestClient(
            env.app,
            client=("testclient", 50000),
            raise_server_exceptions=False,
            headers=dict(client.headers),
        )
    return client


def _all_events(world: World) -> list[AuditEvent]:
    return [*world.written, *world.events]


def test_the_three_routes_are_guarded_emit_and_use_the_mutation_bucket() -> None:
    """Operator for start and complete, admin for manual, all at instance scope."""
    api = [r for r in routes.router.routes if isinstance(r, APIRoute)]
    expected = {
        routes.start_registration: (Role.OPERATOR, Action.HOST_REGISTRATION_START),
        routes.complete_registration: (Role.OPERATOR, Action.HOST_REGISTRATION_COMPLETE),
        routes.register_host_manually: (Role.ADMIN, Action.HOST_REGISTRATION_MANUAL),
    }
    for endpoint, (role, action) in expected.items():
        (route,) = [r for r in api if r.endpoint is endpoint]
        entries = [
            guards.GUARDED[d.call]
            for d in route.dependant.dependencies
            if d.call in guards.GUARDED and guards.GUARDED[d.call][0] is not None
        ]
        assert entries == [(role, None, False)]
        assert guards.EMITTERS[endpoint] is action
    assert guards.mutations_without_emitter(routes.router.routes) == []


def test_start_returns_the_manifest_and_a_state_and_audits_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No secret in the answer; one success event with the kind."""
    world = World(tmp_path)
    response = _client(monkeypatch, world, OPERATOR).post(START)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["post_url"].startswith("https://github.com/settings/apps/new?state=")
    manifest = json.loads(body["manifest_json"])
    assert manifest["hook_attributes"]["url"] == "https://tb.example/webhooks/github/github.com"
    assert set(body) == {"post_url", "manifest_json", "state"}
    (event,) = _all_events(world)
    assert (event.action, event.outcome, event.resource_type, event.resource_id) == (
        Action.HOST_REGISTRATION_START,
        Outcome.SUCCESS,
        "host",
        "github.com",
    )
    assert event.tenant_id is None
    assert event.after == {"kind": "trustbridge"}


def test_start_then_complete_registers_the_host_without_leaking_a_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The PEM goes to the signer, the webhook secret to the store; none is returned or audited."""
    caplog.set_level(logging.DEBUG)
    world = World(tmp_path)
    client = _client(monkeypatch, world, OPERATOR)
    state = client.post(START).json()["state"]
    response = client.post(COMPLETE, json={"code": CODE, "state": state})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "kind": "trustbridge",
        "host": "github.com",
        "app_id": 123,
        "slug": "trustbridge-github-com",
        "html_url": "https://github.com/apps/trustbridge-github-com",
        "webhook_secret_ref": "store:github-hosts/github.com/webhook-secret",
    }
    assert world.signer_path.read_text() == PEM
    assert world.secrets.items["store:github-hosts/github.com/webhook-secret"] == WEBHOOK_SECRET
    done = [e for e in _all_events(world) if e.action is Action.HOST_REGISTRATION_COMPLETE]
    (event,) = done
    assert event.outcome is Outcome.SUCCESS
    assert event.after == {
        "app_id": 123,
        "app_slug": "trustbridge-github-com",
        "kind": "trustbridge",
        "webhook_secret_ref": "store:github-hosts/github.com/webhook-secret",
    }
    haystack = response.text + repr(_all_events(world)) + caplog.text
    for canary in (PEM, "BEGIN", WEBHOOK_SECRET, CODE):
        assert canary not in haystack
    # The GitHub client did log the conversion request, with the code masked.
    assert "/app-manifests/[MASKED]/conversions" in caplog.text


def test_a_state_cannot_be_reused_or_used_by_another_caller_or_after_it_expires(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """400 for each, with a failure event; nothing is stored."""
    world = World(tmp_path)
    client = _client(monkeypatch, world, OPERATOR)
    state = client.post(START).json()["state"]
    stranger = _client(monkeypatch, world, OPERATOR)
    stranger.headers["Authorization"] = "Bearer " + json.dumps(
        {
            "sub": "someone-else",
            "iss": "https://idp.example",
            "resource_access": {"api": {"roles": [OPERATOR]}},
        }
    )
    problem(stranger.post(COMPLETE, json={"code": CODE, "state": state}), 400, "bad-request")
    world.now += 7200
    problem(client.post(COMPLETE, json={"code": CODE, "state": state}), 400, "bad-request")
    world.now -= 7200
    assert client.post(COMPLETE, json={"code": CODE, "state": state}).status_code == 200
    world.state = HostState.UNREGISTERED
    world.stored = None
    problem(client.post(COMPLETE, json={"code": CODE, "state": state}), 400, "bad-request")
    problem(client.post(COMPLETE, json={"code": CODE, "state": "x.y"}), 400, "bad-request")
    failures = [e for e in _all_events(world) if e.outcome is Outcome.FAILURE]
    assert len(failures) == 4
    assert {e.action for e in failures} == {Action.HOST_REGISTRATION_COMPLETE}


def test_a_code_github_refuses_is_a_400(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The conversion failed; the key is not imported and nothing is stored."""
    world = World(tmp_path)
    world.gh.route("POST", f"/app-manifests/{CODE}/conversions", 404, {"message": "Not Found"})
    client = _client(monkeypatch, world, OPERATOR)
    state = client.post(START).json()["state"]
    problem(client.post(COMPLETE, json={"code": CODE, "state": state}), 400, "bad-request")
    assert not world.signer_path.exists()
    assert world.secrets.items == {}


def test_manual_registration_stores_the_app_and_never_echoes_the_key_or_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Admin only; the same storage; one success event with ids and the reference."""
    caplog.set_level(logging.DEBUG)
    world = World(tmp_path)
    body = {"app_id": 4242, "pem": PEM, "webhook_secret": WEBHOOK_SECRET, "slug": "my-app"}
    response = _client(monkeypatch, world, ADMIN).post(MANUAL, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["app_id"] == 4242
    assert world.signer_path.read_text() == PEM
    (event,) = _all_events(world)
    assert (event.action, event.outcome) == (Action.HOST_REGISTRATION_MANUAL, Outcome.SUCCESS)
    assert event.after is not None
    assert event.after["app_id"] == 4242
    haystack = response.text + repr(_all_events(world)) + caplog.text
    for canary in (PEM, "BEGIN", WEBHOOK_SECRET):
        assert canary not in haystack


def test_manual_registration_needs_instance_admin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An operator, a viewer and a tenant-scoped admin are 403; nothing is written."""
    world = World(tmp_path)
    body = {"app_id": 1, "pem": PEM, "webhook_secret": WEBHOOK_SECRET}
    for role in (OPERATOR, VIEWER, ACME_ADMIN):
        problem(_client(monkeypatch, world, role).post(MANUAL, json=body), 403, "forbidden")
    for role in (VIEWER, ACME_ADMIN):
        problem(_client(monkeypatch, world, role).post(START), 403, "forbidden")
    assert not world.signer_path.exists()
    assert _all_events(world) == []


def test_a_bad_body_is_a_422_that_names_fields_and_never_echoes_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The PEM and the secret stay out of the validation detail, even for a missing field."""
    world = World(tmp_path)
    client = _client(monkeypatch, world, ADMIN)
    bad = [
        {"pem": PEM, "webhook_secret": WEBHOOK_SECRET},
        {"app_id": -1, "pem": PEM, "webhook_secret": WEBHOOK_SECRET},
        {"app_id": 1, "pem": PEM, "webhook_secret": WEBHOOK_SECRET, "extra": 1},
        {"app_id": 1, "pem": "x" * 9000, "webhook_secret": WEBHOOK_SECRET},
    ]
    for body in bad:
        response = client.post(MANUAL, json=body)
        problem(response, 422, "validation")
        assert PEM not in response.text
        assert WEBHOOK_SECRET not in response.text
        assert "x" * 100 not in response.text
    not_json = client.post(MANUAL, content=b"{not json" + WEBHOOK_SECRET.encode())
    problem(not_json, 422, "validation")
    assert WEBHOOK_SECRET not in not_json.text
    problem(client.post(COMPLETE, json={"state": "s"}), 422, "validation")
    assert _all_events(world) == []


def test_a_body_over_the_bound_is_a_413(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`ADMIN_MAX_BODY_BYTES` applies to the secret-bearing routes too."""
    world = World(tmp_path)
    body = {"app_id": 1, "pem": "p" * 20000, "webhook_secret": "s"}
    problem(_client(monkeypatch, world, ADMIN).post(MANUAL, json=body), 413, "payload-too-large")


def test_a_host_that_already_has_an_app_is_a_409_with_a_failure_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Configured with an App, ready in this replica, or registered in the database."""
    body = {"app_id": 1, "pem": PEM, "webhook_secret": WEBHOOK_SECRET}
    cases: dict[str, Callable[[World], None]] = {
        "configured": lambda w: setattr(w, "config", make_host_config(app_id=5)),
        "ready": lambda w: setattr(w, "state", HostState.READY),
        "stored": lambda w: setattr(w, "stored", "github.com"),
    }
    for name, arrange in cases.items():
        world = World(tmp_path / name)
        (tmp_path / name).mkdir()
        world.signers = {"main": FileSigner(tmp_path / name / "app.pem")}
        arrange(world)
        client = _client(monkeypatch, world, ADMIN)
        problem(client.post(MANUAL, json=body), 409, "conflict")
        problem(_client(monkeypatch, world, OPERATOR).post(START), 409, "conflict")
        assert not (tmp_path / name / "app.pem").exists()
        assert [e.outcome for e in world.written] == [Outcome.FAILURE, Outcome.FAILURE]


def test_a_registration_that_appears_meanwhile_is_not_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The insert finds the row taken: 409 and a failure event, no success event."""
    world = World(tmp_path)
    world.insert_wins = False
    body = {"app_id": 1, "pem": PEM, "webhook_secret": WEBHOOK_SECRET}
    problem(_client(monkeypatch, world, ADMIN).post(MANUAL, json=body), 409, "conflict")
    assert world.events == []
    (failure,) = world.written
    assert failure.outcome is Outcome.FAILURE


def test_a_signer_that_cannot_import_is_a_409_and_a_bad_key_a_422(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`import it out of band`, and a PEM the signer cannot read."""
    world = World(tmp_path)
    world.signers = {"main": KeySigner()}
    body = {"app_id": 1, "pem": PEM, "webhook_secret": WEBHOOK_SECRET}
    problem(_client(monkeypatch, world, ADMIN).post(MANUAL, json=body), 409, "conflict")
    problem(_client(monkeypatch, world, OPERATOR).post(START), 409, "conflict")
    world.signers = {"main": FileSigner(tmp_path / "app.pem")}
    garbage = {"app_id": 1, "pem": "not a key", "webhook_secret": WEBHOOK_SECRET}
    response = _client(monkeypatch, world, ADMIN).post(MANUAL, json=garbage)
    problem(response, 422, "validation")
    assert "not a key" not in response.text


def test_an_unknown_host_is_a_404_and_a_missing_runtime_a_503(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """404 for a host that is not configured; 503 when registration is not configured."""
    world = World(tmp_path)
    client = _client(monkeypatch, world, OPERATOR)
    problem(client.post("/admin/v1/hosts/nope/registration"), 404, "not-found")
    assert [e.outcome for e in world.written] == [Outcome.FAILURE]
    for path, body in ((START, None), (COMPLETE, {"code": "c", "state": "s"})):
        offline = _client(monkeypatch, world, OPERATOR, runtime=False)
        problem(offline.post(path, json=body), 503, "unavailable")
    admin = _client(monkeypatch, world, ADMIN, runtime=False)
    problem(
        admin.post(MANUAL, json={"app_id": 1, "pem": PEM, "webhook_secret": "s"}),
        503,
        "unavailable",
    )


def test_start_without_an_audit_writer_is_a_503(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The success event cannot be written, so the nonce is not handed out."""
    world = World(tmp_path)
    problem(_client(monkeypatch, world, OPERATOR, writer=False).post(START), 503, "unavailable")


def test_an_unexpected_error_leaves_a_failure_event_and_a_failing_writer_hides_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The secret store is down: 500 and one failure event; if that fails too, it is logged."""
    world = World(tmp_path)
    world.secrets.fail = True
    body = {"app_id": 1, "pem": PEM, "webhook_secret": WEBHOOK_SECRET}
    client = _client(monkeypatch, world, ADMIN, server_errors=False)
    problem(client.post(MANUAL, json=body), 500)
    assert [e.outcome for e in world.written] == [Outcome.FAILURE]
    world.writer_error = OSError("audit down")
    problem(client.post(MANUAL, json=body), 500)
    assert "could not record the failed host.registration.manual event" in caplog.text
    assert WEBHOOK_SECRET not in caplog.text
    assert PEM not in caplog.text


async def test_the_nonce_stores_hand_out_each_nonce_once() -> None:
    """In memory and over Valkey (`SET EX`, `GETDEL`)."""
    memory = MemoryNonceStore()
    await memory.issue("n1", 60)
    assert [await memory.consume("n1"), await memory.consume("n1"), await memory.consume("x")] == [
        True,
        False,
        False,
    ]
    fake = FakeRedis()
    valkey = ValkeyNonceStore(as_redis(fake))
    await valkey.issue("n2", 3600)
    assert fake.expiries["tb:registration:nonce:n2"] == 3600
    assert [await valkey.consume("n2"), await valkey.consume("n2")] == [True, False]


async def test_the_flow_offers_only_trustbridge_apps(tmp_path: Path) -> None:
    """An Argo CD start is refused, and the flow has no connection store to write to."""
    world = World(tmp_path)
    flow_deps = world.runtime().deps
    flow = ManifestFlow(flow_deps)
    argocd = StartRequest(AppKind.ARGOCD, "s", owner="acme", tenant_id=UUID(int=1))
    with pytest.raises(ManifestFlowError, match="argocd"):
        await flow.start(world.config, argocd)
    with pytest.raises(RuntimeError, match="never writes connection data"):
        await flow_deps.connections.put(UUID(int=1), "argocd", {})
    with pytest.raises(RuntimeError, match="audited sink"):
        await flow_deps.registrations.save("github.com", None)  # type: ignore[arg-type]


def test_the_manual_model_hides_its_secrets_in_repr() -> None:
    """A `SecretStr` never prints."""
    model = parse_body(
        json.dumps({"app_id": 1, "pem": PEM, "webhook_secret": WEBHOOK_SECRET}).encode(), ManualIn
    )
    assert PEM not in repr(model)
    assert WEBHOOK_SECRET not in repr(model)
    with pytest.raises(ValidationError):
        ManualIn.model_validate({"app_id": 0, "pem": "p", "webhook_secret": "s"})


def test_the_runtime_is_built_only_when_the_base_url_and_state_key_are_set(
    tmp_path: Path,
) -> None:
    """Both unset: no registration runtime (the routes are a 503)."""
    world = World(tmp_path)
    monitor = SimpleNamespace(
        statuses=lambda: {"github.com": SimpleNamespace(state=HostState.READY)}
    )
    core: Any = SimpleNamespace(
        runtime=SimpleNamespace(monitor=monitor, signers=world.signers), secrets=world.secrets
    )
    assert _registration_runtime(make_settings(), core, None) is None
    only_url = make_settings(public_base_url="https://tb.example")
    assert _registration_runtime(only_url, core, None) is None
    settings = make_settings(public_base_url="https://tb.example", registration_state_key="k" * 32)
    runtime = _registration_runtime(settings, core, None)
    assert runtime is not None
    assert runtime.states() == {"github.com": HostState.READY}
    assert set(runtime.hosts) == {h.name for h in settings.github_hosts}
    assert isinstance(runtime.deps.nonces, MemoryNonceStore)
    shared = _registration_runtime(settings, core, as_redis(FakeRedis()))
    assert shared is not None
    assert isinstance(shared.deps.nonces, ValkeyNonceStore)
    with pytest.raises(ValidationError):
        make_settings(registration_state_key="short")


def test_a_registration_takes_the_claim_before_it_writes_and_releases_it_after(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """lock, then the secret write, then the claim's transaction ends."""
    world = World(tmp_path)
    world.secrets.trace = world.trace
    body = {"app_id": 1, "pem": PEM, "webhook_secret": WEBHOOK_SECRET}
    assert _client(monkeypatch, world, ADMIN).post(MANUAL, json=body).status_code == 200
    assert world.trace.index("lock") < world.trace.index("secret")
    assert world.trace[-1] == "unlock"


def test_a_registration_that_loses_the_claim_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """409 and a failure event; the key file and the secret store are untouched."""
    world = World(tmp_path)
    world.lock_free = False
    client = _client(monkeypatch, world, ADMIN)
    started = _client(monkeypatch, world, OPERATOR).post(START)
    assert started.status_code == 200  # `start` writes nothing, so it needs no claim
    body = {"app_id": 1, "pem": PEM, "webhook_secret": WEBHOOK_SECRET}
    problem(client.post(MANUAL, json=body), 409, "conflict")
    state = started.json()["state"]
    problem(client.post(COMPLETE, json={"code": CODE, "state": state}), 409, "conflict")
    assert not world.signer_path.exists()
    assert world.secrets.items == {}
    failed = [e for e in world.written if e.outcome is Outcome.FAILURE]
    assert [e.action for e in failed] == [
        Action.HOST_REGISTRATION_MANUAL,
        Action.HOST_REGISTRATION_COMPLETE,
    ]


def test_the_http_clients_request_log_hides_the_manifest_code() -> None:
    """The filter rewrites the message and keeps the record; other messages pass untouched."""
    mask = MaskManifestCode()
    record = logging.LogRecord(
        "httpx2",
        logging.INFO,
        __file__,
        1,
        'HTTP Request: %s %s "%s"',
        ("POST", "https://api.github.com/app-manifests/c0de-1234/conversions", "HTTP/1.1 201"),
        None,
    )
    assert mask.filter(record) is True
    assert record.getMessage() == (
        "HTTP Request: POST https://api.github.com/app-manifests/[MASKED]/conversions "
        '"HTTP/1.1 201"'
    )
    plain = logging.LogRecord("httpx2", logging.INFO, __file__, 1, "other %s", ("x",), None)
    assert mask.filter(plain) is True
    assert plain.getMessage() == "other x"
    for name in ("httpx2", "httpx"):
        filters = logging.getLogger(name).filters
        assert sum(isinstance(f, MaskManifestCode) for f in filters) == 1
