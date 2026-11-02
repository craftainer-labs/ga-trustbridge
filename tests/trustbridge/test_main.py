"""The wrapping lifespan and the real `trustbridge.main:app` (0028 invariants 16, 20; 0030 inv. 7).

The wrapper is exercised on a synthetic template app (no side effects on the shared
`app.main:app`); the smoke test then imports the real entrypoint in a subprocess so its
module-level mutations of the template app cannot leak into the template's own tests.
"""

import asyncio
import json
import os
import subprocess
import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from craftainer_observability.health.registry import HealthRegistry
from fastapi import FastAPI
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import trustbridge.main as main_module
from tests.trustbridge.fakes import FakeHostChecker, FakeRedis, FakeSecretStore
from tests.trustbridge.helpers import SIGNERS, as_redis, host, make_settings, sign
from trustbridge.github.availability import HostState
from trustbridge.github.hosts import GitHubHostConfig
from trustbridge.main import Overrides, install
from trustbridge.worker.schema import REQUIRED_SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[2]


def _template(events: list[str]) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]  # noqa: ANN202
        events.append("template-start")
        yield
        events.append("template-end")

    return FastAPI(lifespan=lifespan)


async def test_startup_checks_run_before_the_template_and_resources_outlive_it(
    key_signers: None,
) -> None:
    """Startup checks run before the template and resources outlive it."""
    events: list[str] = []
    checker = FakeHostChecker(failing={"bad"})

    class Spy(FakeHostChecker):
        async def get_app_id(self, host: GitHubHostConfig, jwt: str) -> int:
            """Record the check, then answer like the wrapped checker."""
            events.append(f"check:{host.name}")
            return await checker.get_app_id(host, jwt)

    app = _template(events)
    settings = make_settings(
        [host("ok"), host("bad"), host("new", app_id=None, webhook_secret_ref=None)]
    )
    install(app, settings, Overrides(checker=Spy(), redis=as_redis(FakeRedis())))
    async with app.router.lifespan_context(app):
        assert events[-1] == "template-start"
        assert sorted(e for e in events if e.startswith("check:")) == ["check:bad", "check:ok"]
        hosts = app.state.trustbridge.monitor
        # a failed check never stops the process (0030 invariant 7); the host is just unavailable
        assert hosts.status("bad").state is HostState.UNAVAILABLE
        assert hosts.status("ok").state is HostState.READY
        assert hosts.status("new").state is HostState.UNREGISTERED
        assert {"/webhooks/github/{host}", "/webhooks/argocd/{tenant_id}"} <= set(
            app.openapi()["paths"]
        )
        assert app.state.trustbridge.argocd.handler is None  # until NG5d supplies a state source
    assert events[-1] == "template-end"
    assert not hasattr(app.state, "trustbridge")  # released after the template shut down


async def test_the_webhook_route_works_through_the_wrapped_app_with_valkey_down(
    key_signers: None,
) -> None:
    """The webhook route works through the wrapped app with valkey down."""
    app = _template([])
    valkey = FakeRedis()
    secret_store = FakeSecretStore({"env:SECRET_OK": "s3cret"})
    settings = make_settings([host("ok")])
    install(
        app,
        settings,
        Overrides(
            checker=FakeHostChecker(),
            redis=as_redis(valkey),
            secret_reader=secret_store,
        ),
    )
    valkey.down = True  # the limiter fails open, the relay fails quietly: the outbox still takes it
    body = json.dumps({"zen": "x"}).encode()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("203.0.113.5", 1))
        async with httpx.AsyncClient(transport=transport, base_url="http://tb") as client:
            response = await client.post(
                "/webhooks/github/ok",
                content=body,
                headers={
                    "x-github-event": "ping",
                    "x-github-delivery": "abc-1",
                    "x-hub-signature-256": sign(b"s3cret", body),
                },
            )
        assert response.status_code == 202
        assert (await app.state.trustbridge.store.get("abc-1")) is not None


async def test_the_lifespan_can_be_entered_repeatedly_registering_each_check_once(
    key_signers: None,
) -> None:
    """The lifespan can be entered repeatedly registering each check once."""
    registry = HealthRegistry()  # 0028 invariant 16
    app = _template([])

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def execute(self, _stmt: object) -> types.SimpleNamespace:
            return types.SimpleNamespace(scalar_one_or_none=lambda: REQUIRED_SCHEMA_VERSION)

    settings = make_settings(mode="dev", allow_mock_mode=False)
    install(
        app,
        settings,
        Overrides(
            checker=FakeHostChecker(),
            redis=as_redis(FakeRedis()),
            session_factory=Session,  # type: ignore[arg-type]
            health_registry=lambda: registry,
        ),
    )
    for _ in range(3):
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0)
    results = await registry.run_all()
    assert [r.name for r in results] == ["schema"]
    assert results[0].healthy


def test_install_is_idempotent_and_adds_the_proxy_middleware_once() -> None:
    """Install is idempotent and adds the proxy middleware once."""
    app = _template([])
    settings = make_settings(trusted_proxies=["10.0.0.0/8"])
    assert install(app, settings) is app
    routes = len(app.routes)
    assert install(app, settings) is app
    assert len(app.routes) == routes  # a second install adds nothing
    assert "/webhooks/github/{host}" in app.openapi()["paths"]
    proxies = [m for m in app.user_middleware if cast("Any", m.cls) is ProxyHeadersMiddleware]
    assert len(proxies) == 1
    assert proxies[0].kwargs["trusted_hosts"] == ["10.0.0.0/8"]


def test_unset_trusted_proxies_outside_production_trusts_loopback_only() -> None:
    """Unset trusted proxies outside production trusts loopback only."""
    app = _template([])
    install(app, make_settings())
    (proxy,) = (m for m in app.user_middleware if cast("Any", m.cls) is ProxyHeadersMiddleware)
    assert proxy.kwargs["trusted_hosts"] == "127.0.0.1"


def test_install_fails_loudly_if_the_template_stops_exposing_lifespan_context() -> None:  # inv. 20
    """Install fails loudly if the template stops exposing lifespan context."""
    fake = types.SimpleNamespace(router=types.SimpleNamespace(), state=types.SimpleNamespace())
    with pytest.raises(RuntimeError, match="lifespan_context"):
        install(fake, make_settings())  # type: ignore[arg-type]


def test_install_works_on_an_app_whose_middleware_stack_was_already_built() -> None:
    """Install works on an app whose middleware stack was already built."""
    app = _template([])
    app.middleware_stack = app.build_middleware_stack()  # as after the template's own tests ran
    install(app, make_settings())
    assert app.middleware_stack is None


_SMOKE = """
import asyncio
from trustbridge.main import app

async def main() -> None:
    assert hasattr(app.router, "lifespan_context")
    async with app.router.lifespan_context(app):
        runtime = app.state.trustbridge
        statuses = runtime.monitor.statuses()
        assert statuses, "the startup checks did not run"
        assert all(s.state.value in ("ready", "unavailable") for s in statuses.values()), statuses
        assert {"/webhooks/github/{host}", "/webhooks/argocd/{tenant_id}"} <= set(
            app.openapi()["paths"]
        )

asyncio.run(main())
print("SMOKE-OK")
"""


def test_smoke_the_real_entrypoint_enters_its_lifespan_and_serves_the_route() -> None:  # inv. 20
    """Smoke the real entrypoint enters its lifespan and serves the route."""
    env = {
        **os.environ,
        "MODE": "mock",
        "ALLOW_MOCK_MODE": "1",
        "RUN_MIGRATIONS_ON_STARTUP": "false",
        "GC_FREEZE": "false",
        "GITHUB_HOSTS": json.dumps([host().model_dump(mode="json")]),
        "SIGNERS": json.dumps(SIGNERS),
    }
    env.pop("STATS_CACHE_TTL_SECONDS", None)
    done = subprocess.run(  # noqa: S603 -- our own interpreter and script
        [sys.executable, "-c", _SMOKE],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-3000:]
    assert "SMOKE-OK" in done.stdout


def test_the_app_attribute_is_built_on_first_access_and_nothing_else_is_exported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importing the module builds nothing; `app` installs on the template once."""
    template = _template([])
    monkeypatch.setattr(main_module, "template_app", template)
    monkeypatch.setattr(main_module, "get_trustbridge_settings", make_settings)
    built = main_module.app
    assert built is template
    assert main_module.app is template  # installed once
    with pytest.raises(AttributeError):
        _ = main_module.nothing_else


async def test_an_api_without_an_injected_valkey_opens_and_closes_its_own(
    monkeypatch: pytest.MonkeyPatch, key_signers: None
) -> None:
    """The lifespan owns the client it opened and closes it after the template shut down."""
    closed: list[bool] = []

    class Owned(FakeRedis):
        async def aclose(self) -> None:
            closed.append(True)

    monkeypatch.setattr(main_module, "open_redis", lambda _settings: as_redis(Owned()))
    app = _template([])

    class Session:
        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def execute(self, _stmt: object) -> types.SimpleNamespace:
            return types.SimpleNamespace(scalar_one_or_none=lambda: REQUIRED_SCHEMA_VERSION)

    install(
        app,
        make_settings(mode="dev", allow_mock_mode=False),
        Overrides(
            checker=FakeHostChecker(),
            session_factory=Session,  # type: ignore[arg-type]
            health_registry=HealthRegistry,
        ),
    )
    async with app.router.lifespan_context(app):
        assert closed == []
    assert closed == [True]
