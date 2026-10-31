"""Unit test: register_problem_handlers turns errors into application/problem+json."""

import logging

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.problem_details import register_problem_handlers


@pytest.fixture
def problem_app() -> FastAPI:
    """A throwaway FastAPI app with the problem-details handlers registered."""
    app = FastAPI()
    register_problem_handlers(app)

    @app.get("/boom-http")
    def boom_http() -> None:
        raise HTTPException(404, "not found here")

    @app.get("/boom-challenge")
    def boom_challenge() -> None:
        raise HTTPException(401, "nope", headers={"WWW-Authenticate": 'Bearer realm="x"'})

    @app.get("/boom-validation")
    def boom_validation(required: int) -> dict[str, int]:
        return {"required": required}

    @app.get("/boom-unhandled")
    def boom_unhandled() -> None:
        raise ValueError("something broke")

    return app


def test_http_exception_headers_are_kept(problem_app: FastAPI) -> None:
    """A 401's WWW-Authenticate challenge survives the problem-details rendering."""
    response = TestClient(problem_app).get("/boom-challenge")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer realm="x"'
    assert response.headers["content-type"] == "application/problem+json"


def test_http_exception_becomes_problem_detail(problem_app: FastAPI) -> None:
    """A raised HTTPException renders as application/problem+json with matching fields."""
    response = TestClient(problem_app, raise_server_exceptions=False).get("/boom-http")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["status"] == 404
    assert body["title"] == "Not Found"
    assert body["detail"] == "not found here"
    assert body["instance"] == "/boom-http"
    assert body["type"] == "about:blank"


def test_validation_error_becomes_problem_detail(problem_app: FastAPI) -> None:
    """A request validation failure renders as application/problem+json."""
    response = TestClient(problem_app, raise_server_exceptions=False).get("/boom-validation")
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["status"] == 422
    assert isinstance(body["detail"], list)


def test_unhandled_exception_becomes_generic_problem_detail(
    problem_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """An unhandled exception renders as a generic 500 problem-details body, and is logged."""
    with caplog.at_level(logging.ERROR, logger="app.problem_details"):
        response = TestClient(problem_app, raise_server_exceptions=False).get("/boom-unhandled")
    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["status"] == 500
    assert "Unhandled exception for /boom-unhandled" in caplog.text
    assert "ValueError: something broke" in caplog.text
    assert body["title"] == "Internal Server Error"
