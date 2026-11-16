"""A canary secret planted through a whole tenant deletion appears nowhere (ADR 0034 inv. 11).

Runs in CI's stack (integration). The setup is `test_full_flow_integration.py`'s: the admin
routes (grace 0), the engine over the real store, drain port and database step, and the real
worker over the real outbox, with fake platform and GitHub ports. A canary value is planted where
a secret could travel: the credential core and the connection data the fakes hold, the headers and
body of webhook deliveries (before, during and after the deletion), the reason of the request, and
the error text of a failing platform step. After the run, the canary must not appear in any log
record (message, arguments, extras and formatted exceptions, from every logger), any HTTP response
(problem bodies included), any audit row, any deletion record or any delivery row.
"""

import json
import logging
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text

from tests.trustbridge.admin.test_tenant_deletion_integration import SKIP_FUNCTION, _client
from tests.trustbridge.deletion.test_database_integration import ENVIRONMENT_INSERT, _seed
from tests.trustbridge.deletion.test_flow_integration import _Connections, _Credentials, _Github
from tests.trustbridge.deletion.test_flow_integration import _Platform as _FakePlatform
from tests.trustbridge.deletion.test_full_flow_integration import (
    _assert_cleared,
    _assert_gone,
    _delivery,
    _engine,
    _lapse,
    _request_deletion,
    _run,
    _Then,
    _Webhooks,
)
from tests.trustbridge.tenancy.test_deletion_db_integration import (
    NEW_FUNCTIONS,
    Tenant,
    _admin_scalar,
    _tenant,
)
from tests.trustbridge.tenancy.test_rls_integration import OWNER_ROLE, Db, _provision
from trustbridge.deletion.database import SqlDatabaseStep
from trustbridge.deletion.drain import DrainStep
from trustbridge.deletion.engine import EngineSteps
from trustbridge.deletion.github import GithubDeletionStep
from trustbridge.deletion.platform import AdapterPlatformStep
from trustbridge.deletion.ports import PlatformItem
from trustbridge.tenancy import install_tenant_hook
from trustbridge.worker.deliveries import NewDelivery
from trustbridge.worker.drain_deletion import SqlWritersPort

pytestmark = pytest.mark.integration

CANARY = "canary-secret-7f3a9c1e"


@pytest.fixture(scope="module")
async def db() -> AsyncIterator[Db]:
    """A scratch database of its own, with the functions owned by the owner role."""
    async for provisioned in _provision(enforce=True):
        async with provisioned.admin.begin() as conn:
            for function in (*NEW_FUNCTIONS, SKIP_FUNCTION):
                await conn.execute(text(f"ALTER FUNCTION {function} OWNER TO {OWNER_ROLE}"))
            await conn.execute(text(f"ALTER TABLE github_environments OWNER TO {OWNER_ROLE}"))
            await conn.execute(text("ALTER TABLE github_environments ENABLE ROW LEVEL SECURITY"))
            await conn.execute(text("ALTER TABLE github_environments FORCE ROW LEVEL SECURITY"))
            for seeded in (provisioned.a, provisioned.b):
                await conn.execute(text(ENVIRONMENT_INSERT), {"t": seeded, "k": "seed", "n": 1})
        yield provisioned


@pytest.fixture(scope="module", autouse=True)
def _hook() -> None:
    install_tenant_hook()


class _Records(logging.Handler):
    """Every record any logger emits, flattened to text: message, arguments, extras, traceback."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        fields = {**record.__dict__, "message": record.getMessage()}
        if record.exc_info:
            fields["traceback"] = logging.Formatter().formatException(record.exc_info)
        self.lines.append(json.dumps(fields, default=repr))


class _Capture:
    """Attach `_Records` to the root logger at DEBUG for the duration of the block."""

    def __init__(self) -> None:
        self.records = _Records()
        self._level = 0

    def __enter__(self) -> _Records:
        root = logging.getLogger()
        self._level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self.records)
        return self.records

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        root = logging.getLogger()
        root.removeHandler(self.records)
        root.setLevel(self._level)


@pytest.fixture
def logs() -> Iterator[_Records]:
    """The records of the test, from every logger."""
    with _Capture() as records:
        yield records


class _CanaryCredentials(_Credentials):
    """The credential core, holding a secret it must never hand on."""

    def __init__(self, db: Db) -> None:
        super().__init__(db)
        self.secret = CANARY


class _CanaryConnections(_Connections):
    """Operator-owned connection data: only the name may leave."""

    def __init__(self) -> None:
        self.data = {"argocd": {"url": "https://argocd.example", "token": CANARY}}


class _FailingPlatform(_FakePlatform):
    """The fake platform whose first removal fails with the canary in the error text."""

    def __init__(self) -> None:
        super().__init__()
        self.failures = 0

    async def remove(self, tenant_id: UUID, deletion_id: UUID, item: PlatformItem) -> None:
        if not self.failures:
            self.failures += 1
            raise ConnectionError(f"registry rejected the call token={CANARY}")
        await super().remove(tenant_id, deletion_id, item)


class _CanaryWebhooks(_Webhooks):
    """The worker harness whose deliveries carry the canary in a header and in the body."""

    async def deliver(self, tenant: Tenant, delivery_id: str) -> None:
        body = json.dumps(
            {"installation": {"id": tenant.installation}, "delivery": delivery_id, "token": CANARY}
        ).encode()
        headers = {"x-hub-signature-256": f"sha256={CANARY}"}
        inserted = await self.store.insert(
            NewDelivery(delivery_id, tenant.host, "push", headers, body, datetime.now(UTC))
        )
        assert inserted
        assert await self.relay.tick() >= 1
        assert await self.worker.run_once() >= 1


def _recording(texts: list[str]) -> Any:  # noqa: ANN401 -- the httpx hook mapping
    async def record(response: httpx.Response) -> None:
        await response.aread()
        texts.append(f"{response.status_code} {response.request.url} {response.text}")

    return {"response": [record]}


async def test_a_canary_planted_through_a_whole_deletion_is_in_no_log_response_or_row(
    db: Db, logs: _Records
) -> None:
    """Request to tombstone with a failing step and late deliveries: the canary leaks nowhere."""
    tenant = await _tenant(db, disabled=False)
    await _seed(db, tenant)
    webhooks = _CanaryWebhooks(db, tenant.host)
    await webhooks.start()
    credentials, platform, github = _CanaryCredentials(db), _FailingPlatform(), _Github()
    held: list[bytes] = []

    async def mid_deletion() -> None:
        await webhooks.deliver(tenant, "mid")
        row = await _delivery(db, tenant, "mid")
        assert row.body is not None
        held.append(bytes(row.body))  # the canary really is in the stored delivery

    async def after_last_pass() -> None:
        await webhooks.deliver(tenant, "late")

    database = _Then(SqlDatabaseStep(db.app, batch_size=3), after_last_pass)
    steps = EngineSteps(
        drain=DrainStep(
            SqlWritersPort(db.app, cast("Any", credentials)),
            poll_seconds=0,
            max_passes=5,
            sleep=_lapse(db, tenant),
        ),
        platform=AdapterPlatformStep(platform, _CanaryConnections()),
        github=_Then(GithubDeletionStep(github), mid_deletion),
        database=database,
    )
    engine = _engine(db, steps)
    responses: list[str] = []

    async with (
        _client(db, "trustbridge:admin") as admin,
        _client(db, "trustbridge:viewer") as view,
    ):
        admin.event_hooks = _recording(responses)
        view.event_hooks = _recording(responses)
        # a viewer's mutation and an enable during the deletion are problem responses
        denied = await view.post(
            f"/admin/v1/tenants/{tenant.id}/disable", json={"reason": f"token={CANARY}"}
        )
        assert denied.status_code == 403
        record = await _request_deletion(
            admin, tenant, delete_platform_resources=True, reason=f"customer asked token={CANARY}"
        )
        refused = await admin.post(f"/admin/v1/tenants/{tenant.id}/enable")
        assert refused.status_code == 409

        for _ in range(6):
            await _run(db, engine)
            shown = await view.get(f"/admin/v1/tenant-deletions/{record['id']}")
            if shown.json()["state"] == "done":
                break
        assert shown.json()["state"] == "done"

    await _assert_gone(db, tenant)
    await _assert_cleared(db, tenant, "mid", "late")
    await webhooks.deliver(tenant, "after")  # dropped, with its canary body
    assert platform.failures == 1  # the failing step ran, and was retried
    assert any(CANARY.encode() in body for body in held)
    assert logs.lines  # something was logged, so the check below is not vacuous

    audit = await _admin_scalar(
        db, "SELECT coalesce(string_agg(row_to_json(a)::text, E'\\n'), '') FROM audit_events a"
    )
    deletions = await _admin_scalar(
        db, "SELECT coalesce(string_agg(row_to_json(d)::text, E'\\n'), '') FROM tenant_deletions d"
    )
    deliveries = await _admin_scalar(
        db,
        "SELECT coalesce(string_agg(host || ' ' || delivery_id || ' ' || status || ' ' || "
        "coalesce(last_error, '') || ' ' || headers::text || ' ' || "
        "coalesce(convert_from(body, 'UTF8'), ''), E'\\n'), '') FROM webhook_deliveries",
    )
    assert "tenant.deletion.requested" in audit  # the audit text is not empty either
    surfaces = {
        "log records": "\n".join(logs.lines),
        "responses": "\n".join(responses),
        "audit rows": audit,
        "deletion records": deletions,
        "delivery rows": deliveries,
    }
    leaked = [name for name, content in surfaces.items() if CANARY in content]
    assert leaked == []
