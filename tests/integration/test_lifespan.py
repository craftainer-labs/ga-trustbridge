"""Integration test: the app's lifespan applies pending Alembic migrations on startup."""

import logging

from fastapi.testclient import TestClient

from app.main import app


def test_lifespan_applies_migrations_and_starts_serving() -> None:
    """Starting the app (lifespan included) against the real database succeeds."""
    with TestClient(app) as client:
        response = client.get("/health/live")
    assert response.status_code == 200


def test_lifespan_does_not_disable_app_loggers() -> None:
    """Running migrations must not silence app.* loggers for the rest of the process.

    alembic/env.py's fileConfig call runs on every real startup (app.main's lifespan);
    its default disable_existing_loggers=True would otherwise permanently disable
    every already-instantiated app.* logger (app.oidc, app.problem_details, ...) not
    declared in alembic.ini's own [loggers] section -- see alembic/env.py's comment.
    """
    with TestClient(app):
        pass
    assert logging.getLogger("app.oidc").disabled is False
