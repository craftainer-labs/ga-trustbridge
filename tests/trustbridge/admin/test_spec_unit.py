"""The OpenAPI contract: served document, committed snapshot and drift (ADR 0033 inv. 11, 12)."""

import importlib
import json
import runpy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.trustbridge.admin.helpers import build, problem
from trustbridge.admin import routes

SNAPSHOT = Path(__file__).resolve().parents[3] / "docs" / "openapi" / "admin-v1.json"
REGENERATE = "python -m trustbridge.admin.spec > docs/openapi/admin-v1.json"


def test_the_committed_snapshot_equals_the_generated_spec() -> None:
    """Inv. 11: a route or schema change without a snapshot update fails here."""
    assert SNAPSHOT.read_text() == routes.snapshot_text(), f"run: {REGENERATE}"


def test_the_spec_lists_only_admin_routes_and_needs_a_token() -> None:
    """Inv. 11 (second half): generated from the admin router only, authenticated."""
    env = build()
    problem(TestClient(env.app).get("/admin/v1/openapi.json"), 401, "unauthenticated")
    response = env.client().get("/admin/v1/openapi.json")
    assert response.headers["content-type"] == "application/json"
    spec = response.json()
    assert spec["openapi"].startswith("3.")
    assert spec["paths"]
    assert all(path.startswith("/admin/v1/") for path in spec["paths"])
    assert "/other" not in spec["paths"]
    assert spec == json.loads(SNAPSHOT.read_text())


def test_the_snapshot_documents_every_read_route_of_adr_0033() -> None:
    """The v1 read side is complete except `/tenant-deletions` (NG6h, ADR 0034); the only
    writes so far are tenant disable and enable (NG6e)."""
    paths = set(json.loads(SNAPSHOT.read_text())["paths"])
    base = "/admin/v1"
    assert {
        f"{base}{p}"
        for p in (
            "/me",
            "/openapi.json",
            "/hosts",
            "/hosts/{host}",
            "/tenants",
            "/tenants/{tenant_id}",
            "/tenants/{tenant_id}/repositories",
            "/tenants/{tenant_id}/adapters",
            "/tenants/{tenant_id}/resources",
            "/tenants/{tenant_id}/credentials",
            "/tenants/{tenant_id}/operations",
            "/deliveries",
            "/deliveries/{host}/{delivery_id}",
            "/tenants/{tenant_id}/audit-events",
            "/tenants/{tenant_id}/audit-events/export",
            "/audit-events",
            "/audit-events/{event_id}",
            "/audit-events/export",
            "/tenants/{tenant_id}/disable",
            "/tenants/{tenant_id}/enable",
        )
    } == paths


def test_the_spec_is_served_from_one_immutable_body() -> None:
    """The built document is bytes: a caller cannot change what the next caller gets."""
    assert routes._spec_body() is routes._spec_body()
    assert isinstance(routes._spec_body(), bytes)


def test_the_regeneration_command_prints_the_snapshot(capsys: pytest.CaptureFixture[str]) -> None:
    """The module prints exactly the committed text when run; importing it prints nothing."""
    runpy.run_module("trustbridge.admin.spec", run_name="__main__")
    assert capsys.readouterr().out == SNAPSHOT.read_text()
    importlib.import_module("trustbridge.admin.spec")
    assert capsys.readouterr().out == ""
