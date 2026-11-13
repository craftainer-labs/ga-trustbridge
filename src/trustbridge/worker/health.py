"""Health endpoints of the worker and reconciler processes (NG7b, ADR 0028).

The api answers `/health/live` and `/health/ready` through the template's router. The worker
and reconciler have no HTTP server, so when `HEALTH_PORT` is set they run this one: a minimal
HTTP/1.1 responder on the standard library's asyncio (no dependency), bound to every interface
because the kubelet probes the pod IP. It serves two unauthenticated GET paths and returns a
fixed JSON status, never a detail, a name or an exception text (those go to the log):

- `GET /health/live`: 200 while every registered loop has made progress within its limit (the
  response itself proves the event loop is responsive); 503 when one is stale, so the kubelet
  restarts a process whose loop is stuck. It touches no dependency (NFR-0008), and a process
  that is waiting for the schema has registered no loop yet, so it stays alive.
- `GET /health/ready`: 200 when every readiness check passes (Valkey answers PING; the database
  answers and the schema is current, which one read of `schema_compat` shows), else 503. The
  checks run per probe under a timeout, as the api's do.

A loop's limit is derived from its own interval plus `LEASE_TTL_SECONDS`, the settings' bound
on one unit of work (`stale_after`), so a slow but healthy loop is not killed.
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping

from craftainer_observability.health.redact import log_failure

from trustbridge.settings import TrustbridgeSettings

logger = logging.getLogger(__name__)

LIVE_PATH = "/health/live"
READY_PATH = "/health/ready"
REQUEST_TIMEOUT_SECONDS = 5.0
CHECK_TIMEOUT_SECONDS = 5.0
MAX_LINE_BYTES = 8192
MAX_HEADER_LINES = 100

Check = Callable[[], Awaitable[object]]
"""A readiness check: truthy when healthy, falsy or raising when not."""
_REASONS = {200: "OK", 404: "Not Found", 405: "Method Not Allowed", 503: "Service Unavailable"}


def stale_after(settings: TrustbridgeSettings, interval_seconds: float) -> float:
    """Seconds without progress after which a loop that wakes every `interval_seconds` is stale."""
    return interval_seconds + settings.lease_ttl_seconds


class Heartbeat:
    """The last time each named loop made progress, against a per-loop limit."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """Start with no loops registered (nothing can be stale)."""
        self._clock = clock
        self._limits: dict[str, float] = {}
        self._last: dict[str, float] = {}

    def register(self, name: str, limit_seconds: float) -> None:
        """Track `name`; it counts as having just made progress."""
        self._limits[name] = limit_seconds
        self._last[name] = self._clock()

    def beat(self, name: str) -> None:
        """Record progress of `name` (ignored for a loop that is not registered)."""
        if name in self._limits:
            self._last[name] = self._clock()

    def stale(self) -> list[str]:
        """The names of the loops whose last progress is older than their limit."""
        now = self._clock()
        return sorted(n for n, limit in self._limits.items() if now - self._last[n] > limit)


async def _run_check(name: str, check: Check) -> bool:
    """True if `check` returns a truthy value in time; a raise or timeout is logged and False."""
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
            return bool(await check())
    except Exception as exc:
        log_failure(logger, f"readiness check {name} failed", exc)
        return False


class HealthServer:
    """Serves `/health/live` and `/health/ready` for one process."""

    def __init__(
        self, heartbeat: Heartbeat, ready_checks: Mapping[str, Check], port: int, host: str = ""
    ) -> None:
        """`host` is empty (all interfaces) by default; tests pass `127.0.0.1` and port 0."""
        self._heartbeat = heartbeat
        self._checks = dict(ready_checks)
        self._port = port
        self._host = host
        self._server: asyncio.Server | None = None

    async def start(self) -> int:
        """Start listening; returns the bound port."""
        self._server = await asyncio.start_server(
            self._handle, self._host or None, self._port, limit=MAX_LINE_BYTES
        )
        port: int = self._server.sockets[0].getsockname()[1]
        logger.info("health endpoint listening", extra={"port": port})
        return port

    async def stop(self) -> None:
        """Stop listening and wait for open connections to finish."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Answer one request and close the connection."""
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                status, body = await self._respond(reader)
            payload = json.dumps(body).encode()
            head = (
                f"HTTP/1.1 {status} {_REASONS[status]}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(payload)}\r\n"
                "Cache-Control: no-store\r\nConnection: close\r\n\r\n"
            )
            writer.write(head.encode() + payload)
            await writer.drain()
        except TimeoutError, ConnectionError, ValueError:
            logger.debug("health request dropped", exc_info=True)
        finally:
            writer.close()

    async def _respond(self, reader: asyncio.StreamReader) -> tuple[int, dict[str, object]]:
        """Read the request line (headers are discarded) and pick the answer."""
        line = (await reader.readline()).decode("latin-1").split()
        for _ in range(MAX_HEADER_LINES):  # discard the headers
            if (await reader.readline()).strip() == b"":
                break
        method, path = [*line, "", ""][:2]
        if method != "GET":
            return 405, {"status": "error"}
        if path == LIVE_PATH:
            stale = self._heartbeat.stale()
            if stale:
                logger.error("liveness failing; loops made no progress", extra={"loops": stale})
            return (503, {"status": "fail"}) if stale else (200, {"status": "ok"})
        if path == READY_PATH:
            names = list(self._checks)
            results = await asyncio.gather(*(_run_check(n, self._checks[n]) for n in names))
            checks = {n: ("ok" if ok else "fail") for n, ok in zip(names, results, strict=True)}
            ok = all(results)
            return (200 if ok else 503), {"status": "ok" if ok else "fail", "checks": checks}
        return 404, {"status": "error"}
