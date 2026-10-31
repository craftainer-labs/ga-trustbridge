"""Benchmark: in-process ASGI round trips against the app under MODE=mock (no Postgres)."""

import asyncio

import httpx
from pytest_benchmark.fixture import BenchmarkFixture


def test_health_live_round_trip(
    benchmark: BenchmarkFixture,
    mock_app_client: httpx.AsyncClient,
    event_loop_runner: asyncio.Runner,
) -> None:
    """GET /health/live: framework + middleware overhead with no handler work."""

    def _round_trip() -> httpx.Response:
        return event_loop_runner.run(mock_app_client.get("/health/live"))

    response = benchmark(_round_trip)
    assert response.status_code == 200


def test_crud_list_round_trip(
    benchmark: BenchmarkFixture,
    mock_app_client: httpx.AsyncClient,
    event_loop_runner: asyncio.Runner,
) -> None:
    """GET a crud list (JSON): routing, auth dependency, in-memory repository, serialization."""

    def _round_trip() -> httpx.Response:
        return event_loop_runner.run(mock_app_client.get("/crud/v1/heroes/v2/json"))

    response = benchmark(_round_trip)
    assert response.status_code == 200
    assert response.json()
