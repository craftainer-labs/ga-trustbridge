"""The worker and reconciler health endpoints (NG7b): live, ready, a stale heartbeat."""

import asyncio
import json
import socket
from typing import Any

import pytest

from tests.trustbridge.fakes import FakeHostChecker, FakeRedis
from tests.trustbridge.helpers import as_redis, make_settings
from tests.trustbridge.worker.harness import Harness
from trustbridge.worker import health, runtime
from trustbridge.worker.health import HealthServer, Heartbeat, stale_after
from trustbridge.worker.reconciler import Reconciler, ReconcilerDeps, periodic
from trustbridge.worker.runtime import ProcessOverrides


class Ticker:
    """A controllable monotonic clock."""

    def __init__(self) -> None:
        """Start at 100 s."""
        self.now = 100.0

    def __call__(self) -> float:
        """The current time."""
        return self.now


async def _get(port: int, request: bytes) -> tuple[int, dict[str, object]]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split()[1])
    assert b"Connection: close" in head
    return status, json.loads(body)


def _req(path: str, method: str = "GET") -> bytes:
    return f"{method} {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode()


async def _serve(
    heartbeat: Heartbeat, checks: dict[str, health.Check] | None = None
) -> tuple[HealthServer, int]:
    server = HealthServer(heartbeat, checks or {}, 0, host="127.0.0.1")
    return server, await server.start()


def test_the_limit_is_the_loop_interval_plus_the_lease_ttl() -> None:
    """A slow but healthy loop is not stale: interval plus the bound on one unit of work."""
    settings = make_settings(lease_ttl_seconds=120)
    assert stale_after(settings, 900) == 1020
    assert stale_after(settings, 5) == 125


def test_a_loop_is_stale_only_after_its_limit_and_a_beat_resets_it() -> None:
    """Heartbeat bookkeeping."""
    clock = Ticker()
    beat = Heartbeat(clock)
    assert beat.stale() == []
    beat.register("a", 10)
    beat.register("b", 100)
    beat.beat("unknown")  # ignored
    clock.now += 10
    assert beat.stale() == []
    clock.now += 1
    assert beat.stale() == ["a"]
    beat.beat("a")
    assert beat.stale() == []


async def test_live_is_200_and_turns_503_with_a_stale_heartbeat() -> None:
    """Liveness follows the heartbeat and returns no detail."""
    clock = Ticker()
    beat = Heartbeat(clock)
    server, port = await _serve(beat)
    try:
        assert await _get(port, _req("/health/live")) == (200, {"status": "ok"})  # no loops yet
        beat.register("consume", 30)
        assert await _get(port, _req("/health/live")) == (200, {"status": "ok"})
        clock.now += 31
        assert await _get(port, _req("/health/live")) == (503, {"status": "fail"})
        beat.beat("consume")
        assert await _get(port, _req("/health/live")) == (200, {"status": "ok"})
    finally:
        await server.stop()


async def test_ready_reports_each_check_and_hides_failure_detail() -> None:
    """Readiness is 200 only when every check passes; a raise or a falsy result fails it."""

    async def good() -> bool:
        return True

    async def falsy() -> bool:
        return False

    async def boom() -> bool:
        raise RuntimeError("password=hunter2 host=db.internal")

    server, port = await _serve(Heartbeat(), {"valkey": good, "schema": good})
    try:
        assert await _get(port, _req("/health/ready")) == (
            200,
            {"status": "ok", "checks": {"valkey": "ok", "schema": "ok"}},
        )
    finally:
        await server.stop()
    server, port = await _serve(Heartbeat(), {"valkey": boom, "schema": falsy})
    try:
        status, body = await _get(port, _req("/health/ready"))
        assert (status, body) == (
            503,
            {"status": "fail", "checks": {"valkey": "fail", "schema": "fail"}},
        )
    finally:
        await server.stop()


async def test_a_slow_check_times_out_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hanging dependency fails readiness instead of hanging the probe."""
    monkeypatch.setattr(health, "CHECK_TIMEOUT_SECONDS", 0.05)

    async def hang() -> bool:
        await asyncio.sleep(10)
        return True

    server, port = await _serve(Heartbeat(), {"valkey": hang})
    try:
        assert (await _get(port, _req("/health/ready")))[0] == 503
    finally:
        await server.stop()


async def test_other_paths_and_methods_are_refused() -> None:
    """Only GET of the two health paths is served."""
    server, port = await _serve(Heartbeat())
    try:
        assert await _get(port, _req("/metrics")) == (404, {"status": "error"})
        assert await _get(port, _req("/health/live", "POST")) == (405, {"status": "error"})
        assert (await _get(port, b"\r\n\r\n"))[0] == 405  # not a request line
        many = b"GET /health/live HTTP/1.1\r\n" + b"X: y\r\n" * 150 + b"\r\n"
        assert (await _get(port, many))[0] == 200  # extra headers are discarded
    finally:
        await server.stop()


async def test_a_silent_client_is_dropped_after_the_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client that never finishes its request cannot hold the connection."""
    monkeypatch.setattr(health, "REQUEST_TIMEOUT_SECONDS", 0.05)
    server, port = await _serve(Heartbeat())
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        assert await reader.read() == b""  # closed without a response
        writer.close()
    finally:
        await server.stop()


async def test_stopping_a_server_that_never_started_is_a_no_op() -> None:
    """stop() before start()."""
    await HealthServer(Heartbeat(), {}, 0).stop()


async def test_periodic_beats_around_every_run_even_a_failing_one() -> None:
    """The reconciler's loop reports progress before and after each job."""
    stop = asyncio.Event()
    beats: list[str] = []

    async def job() -> None:
        stop.set()
        raise RuntimeError("boom")

    await periodic("j", 0.001, job, stop, beats.append)
    assert beats == ["j", "j"]


async def test_the_worker_and_reconciler_register_their_loops_with_derived_limits() -> None:
    """Each loop registers a limit from its own interval; the worker beats per iteration."""
    h = await Harness({}).start()
    beat = Heartbeat()
    h.worker._d.heartbeat = beat
    stop = asyncio.Event()

    async def one_iteration() -> int:
        stop.set()
        return 0

    h.worker.run_once = one_iteration  # type: ignore[method-assign]
    await h.worker.run(stop)
    assert set(beat._limits) == {"consume"}
    assert beat._limits["consume"] == stale_after(h.settings, h.settings.worker_block_ms / 1000)

    rbeat = Heartbeat()
    rstop = asyncio.Event()
    rstop.set()
    recon = Reconciler(
        h.settings,
        ReconcilerDeps(
            store=h.store,
            queue=h.queue,
            relay=h.relay,
            hosts=h.hosts,
            leases=h.leases,
            deliveries=None,
            heartbeat=rbeat,
        ),
    )
    await recon.run(rstop)
    assert set(rbeat._limits) == {"relay-tick", "resweep", "deliveries", "retention", "trim"}
    assert rbeat._limits["resweep"] == stale_after(h.settings, h.settings.relay_recheck_seconds)


class BlockingRedis(FakeRedis):
    """Yields to the event loop like a blocking XREADGROUP (the plain fake never does)."""

    async def xreadgroup(self, *args: Any, **kwargs: Any) -> list[Any]:  # noqa: ANN401
        """Sleep, then read."""
        await asyncio.sleep(0.01)
        return await super().xreadgroup(*args, **kwargs)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def test_a_process_serves_health_while_running_when_a_port_is_set(
    key_signers: None,
) -> None:
    """HEALTH_PORT starts the endpoint; readiness reflects Valkey; it stops with the process."""
    port = _free_port()
    redis = BlockingRedis()
    stop = asyncio.Event()
    settings = make_settings(health_port=port, worker_block_ms=10)
    task = asyncio.create_task(
        runtime.run_process(
            "worker",
            settings,
            ProcessOverrides(redis=as_redis(redis), checker=FakeHostChecker(), stop=stop),
        )
    )
    answers: list[tuple[int, dict[str, object]]] = []
    for _ in range(100):
        await asyncio.sleep(0.02)
        try:
            answers.append(await _get(port, _req("/health/live")))
            break
        except OSError:
            continue
    assert answers == [(200, {"status": "ok"})]
    status, body = await _get(port, _req("/health/ready"))
    assert (status, body["checks"]) == (200, {"valkey": "ok", "schema": "ok"})
    redis.down = True
    assert (await _get(port, _req("/health/ready")))[0] == 503
    stop.set()
    assert await task == 0
    with pytest.raises(OSError):
        await _get(port, _req("/health/live"))


def test_health_port_is_off_by_default_and_validated() -> None:
    """HEALTH_PORT unset means no endpoint; the range is checked."""
    assert make_settings().health_port is None
    assert make_settings(health_port=8081).health_port == 8081
    for bad in (0, 70000):
        with pytest.raises(ValueError, match="health_port"):
            make_settings(health_port=bad)
