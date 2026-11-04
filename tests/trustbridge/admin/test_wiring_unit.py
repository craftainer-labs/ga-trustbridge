"""`trustbridge.main` mounts the admin API and builds its runtime (ADR 0033, ADR 0032)."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI

from tests.trustbridge.admin.helpers import fake_factory
from tests.trustbridge.fakes import FakeHostChecker, FakeRedis
from tests.trustbridge.helpers import as_redis, make_settings
from trustbridge import main as main_module
from trustbridge.admin.access import NoTenants, SqlTenantAccess
from trustbridge.admin.runtime import get_admin_runtime
from trustbridge.audit.throttle import ThrottledDenials
from trustbridge.main import Overrides, install
from trustbridge.rbac import RbacRuntime


def _template() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]  # noqa: ANN202
        yield

    return FastAPI(lifespan=lifespan)


async def test_the_router_is_mounted_and_the_runtime_lives_with_the_lifespan(
    key_signers: None,
) -> None:
    """Without a database (mock mode) there is no tenant access and no denial sink."""
    app = _template()
    install(app, make_settings(), Overrides(checker=FakeHostChecker(), redis=as_redis(FakeRedis())))
    assert any(p.startswith("/admin/v1/") for p in app.openapi()["paths"])
    async with app.router.lifespan_context(app):
        runtime = app.state.rbac
        assert isinstance(runtime, RbacRuntime)
        assert isinstance(runtime.access, NoTenants)
        assert runtime.denials is None
        assert runtime.config.client_id == "api"
        admin = get_admin_runtime(app)
        assert admin is not None
        assert admin.session_factory is None
        assert admin.hosts is not None
        assert admin.export_lag_seconds == 30.0
    assert get_admin_runtime(app) is None
    assert not hasattr(app.state, "rbac")


async def test_with_a_database_denials_are_throttled_and_flushed_each_window(
    key_signers: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sink is `ThrottledDenials` and a background task flushes its summaries."""
    flushed = asyncio.Event()

    async def flush(self: ThrottledDenials) -> int:
        flushed.set()
        return 0

    monkeypatch.setattr(ThrottledDenials, "flush", flush)
    app = _template()
    settings = make_settings(audit_denied_window_seconds=1, audit_denied_burst=2)
    install(
        app,
        settings,
        Overrides(
            checker=FakeHostChecker(),
            redis=as_redis(FakeRedis()),
            session_factory=fake_factory(),
        ),
    )
    async with app.router.lifespan_context(app):
        runtime = app.state.rbac
        assert isinstance(runtime.access, SqlTenantAccess)
        assert isinstance(runtime.denials, ThrottledDenials)
        await asyncio.wait_for(flushed.wait(), timeout=5)
        assert get_admin_runtime(app).writer is not None  # type: ignore[union-attr]


async def test_a_failing_flush_is_logged_and_the_loop_goes_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One bad pass must not end the flush task."""
    calls = 0

    class Flaky:
        async def flush(self) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("valkey blip")
            return 0

    task = asyncio.create_task(main_module._flush_denials(Flaky(), 0))  # type: ignore[arg-type]
    await asyncio.sleep(0.05)
    task.cancel()
    assert calls >= 2
    assert "denial summary flush failed" in caplog.text
