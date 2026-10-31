"""Integration test: an open SSE stream vs SIGTERM, and a lossless resume afterwards.

docs/architecture.md, "Kubernetes deployment (future)" / Autoscaling: `/events`
streams never end on their own, so on a scale-down they hold a terminating pod open
until `--timeout-graceful-shutdown` cancels them. That is acceptable only if (a) the
process really exits within that timeout and (b) a client resumes losslessly
elsewhere, through the broker's persistent MQTT session (see
docs/adrs/0015-mqtt-for-crud-events.md; the router falls back from `subscriber_id` to
the `Last-Event-ID` header, which is how a reconnecting EventSource sends it).

Each app instance is a real `uvicorn` subprocess on its own free loopback port
(never 8000, which the devcontainer's own api uses, and not the e2e suite's fixed
ports), in the devcontainer's normal MODE=dev against its own Postgres, Mosquitto and
Keycloak. A subprocess is required: the signal must hit a separate process, and an
in-process ASGI transport can't stream (see test_heroes_v2_events.py's docstring).
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from app.config import get_settings

_GRACEFUL_SHUTDOWN_SECONDS = 3
# Process start-up to exit, beyond the graceful timeout: the lifespan shutdown and
# interpreter teardown. Generous enough not to flake, small enough to catch a hang.
_EXIT_MARGIN_SECONDS = 5.0
_STARTUP_TIMEOUT_SECONDS = 30.0

_EVENTS_URL = "/crud/v1/heroes/v2/json/events"
_HEROES_URL = "/crud/v1/heroes/v2/json"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def _uvicorn(tmp_path: Path, name: str) -> Iterator[tuple[subprocess.Popen[bytes], str]]:
    """Run the real app under uvicorn; yield the process and its base URL once healthy."""
    uvicorn = shutil.which("uvicorn")
    if uvicorn is None:
        raise RuntimeError("uvicorn not found on PATH")
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "MODE": "dev",
        # This test's own traffic must not trip the production-sized rate limits.
        "RATE_LIMIT_MOCK_TOKEN": "1000/minute",
        "RATE_LIMIT_BULK_ACTION": "1000/minute",
        "SSE_KEEPALIVE_SECONDS": "0.2",
        # Both processes sign subscriber ids with one key, so the second accepts the first's.
        "SUBSCRIBER_SECRET": "graceful-shutdown-test-secret-0123456789",
    }
    with (tmp_path / f"{name}.log").open("wb") as log:
        # Fixed args plus a port this function itself picked, not external input.
        process = subprocess.Popen(  # noqa: S603
            [
                uvicorn,
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--timeout-graceful-shutdown",
                str(_GRACEFUL_SHUTDOWN_SECONDS),
            ],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f"{name} exited early; see {tmp_path / f'{name}.log'}")
                try:
                    httpx.get(f"{base_url}/health/live", timeout=1).raise_for_status()
                    break
                except httpx.HTTPError:
                    if time.monotonic() > deadline:
                        raise RuntimeError(
                            f"{name} not healthy in {_STARTUP_TIMEOUT_SECONDS}s"
                        ) from None
                    time.sleep(0.1)
            yield process, base_url
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


def _access_token() -> str:
    """Password-grant login of the dev realm's `maintainer` (password == username)."""
    settings = get_settings()
    # In-network realm address (not the browser-facing oidc_token_url) and the dev-only
    # password-grant client (the public `api` client has direct grants off).
    token_url = (
        settings.discovery_url.removesuffix("/.well-known/openid-configuration")
        + "/protocol/openid-connect/token"
    )
    response = httpx.post(
        token_url,
        data={
            "grant_type": "password",
            "client_id": "test-cli",
            "username": "maintainer",
            "password": "maintainer",
        },
        timeout=10,
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


def _read_frame(lines: Iterator[str]) -> tuple[str, dict[str, object]]:
    """Read the next `id:` / `data:` / blank SSE frame, skipping keep-alive comments."""
    line = next(lines)
    while line.startswith(":") or line == "":
        line = next(lines)
    event_id = line.removeprefix("id: ")
    data = json.loads(next(lines).removeprefix("data: "))
    assert next(lines) == ""
    return event_id, data


def test_sigterm_ends_open_sse_stream_in_time_and_resume_loses_nothing(tmp_path: Path) -> None:
    """SIGTERM with an open /events stream exits within the timeout; a resume gets every event."""
    headers = {"Authorization": f"Bearer {_access_token()}"}
    created_ids: list[int] = []

    with (
        _uvicorn(tmp_path, "first") as (first, first_url),
        httpx.Client(base_url=first_url, headers=headers, timeout=30) as http,
        http.stream("GET", _EVENTS_URL) as stream,
    ):
        assert stream.status_code == 200
        lines = stream.iter_lines()
        event_id, subscriber = _read_frame(lines)
        assert event_id == "0"
        subscriber_id = str(subscriber["subscriber_id"])

        before = http.post(_HEROES_URL, json={"name": "Before SIGTERM", "powers": ["A"]})
        before.raise_for_status()
        created_ids.append(before.json()["id"])
        _, received = _read_frame(lines)
        assert received["record_id"] == created_ids[0]

        # The stream is still open here: SIGTERM must not wait for it forever.
        sent_at = time.monotonic()
        first.send_signal(signal.SIGTERM)
        try:
            first.wait(timeout=_GRACEFUL_SHUTDOWN_SECONDS + _EXIT_MARGIN_SECONDS)
        except subprocess.TimeoutExpired:
            pytest.fail("uvicorn did not exit within the graceful timeout plus margin")
        elapsed = time.monotonic() - sent_at
        assert elapsed <= _GRACEFUL_SHUTDOWN_SECONDS + _EXIT_MARGIN_SECONDS

    with (
        _uvicorn(tmp_path, "second") as (_, second_url),
        httpx.Client(base_url=second_url, headers=headers, timeout=30) as http,
    ):
        try:
            # Produced while the subscriber has no live connection at all.
            between = http.post(_HEROES_URL, json={"name": "Between pods", "powers": ["B"]})
            between.raise_for_status()
            created_ids.append(between.json()["id"])

            with http.stream(
                "GET", _EVENTS_URL, headers={"Last-Event-ID": subscriber_id}
            ) as stream:
                assert stream.status_code == 200
                lines = stream.iter_lines()
                _, resumed = _read_frame(lines)
                assert resumed["subscriber_id"] == subscriber_id
                _, missed = _read_frame(lines)
                assert missed["record_id"] == created_ids[1]
                assert missed["action"] == "create"
        finally:
            # Heroes are owner-scoped, so the creating user can always remove its own.
            for hero_id in created_ids:
                http.delete(_HEROES_URL, params={"id": hero_id})
