"""The audit event model, the NDJSON shape and the audit settings, without a database.

ADR 0032 invariants 5 (identity columns), 12 and 13 (export shape; the Postgres-backed halves
are in `test_audit_integration.py`) and EB-15 (retention settings).
"""

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.trustbridge.helpers import host, make_settings
from trustbridge.audit import (
    MAX_PAGE_SIZE,
    ActorType,
    AuditEvent,
    AuditQuery,
    AuditRecord,
    Outcome,
    export_ndjson,
    list_events,
    purge_expired,
)
from trustbridge.audit import store as store_module

COLUMNS = [
    "id",
    "seq",
    "occurred_at",
    "recorded_at",
    "tenant_id",
    "actor_type",
    "actor_sub",
    "actor_iss",
    "on_behalf_of",
    "source_ip",
    "action",
    "resource_type",
    "resource_id",
    "outcome",
    "before",
    "after",
    "request_id",
    "trace_id",
    "schema_version",
]


def _event(**overrides: Any) -> AuditEvent:  # noqa: ANN401
    values: dict[str, Any] = {
        "actor_type": ActorType.USER,
        "actor_sub": "sub-1",
        "actor_iss": "https://idp.example",
        "action": "tenant.disable",
        "resource_type": "tenant",
        "outcome": Outcome.SUCCESS,
    }
    values.update(overrides)
    return AuditEvent(**values)


def test_a_valid_user_and_system_event_are_accepted() -> None:
    """A user event carries sub and iss; a system event a fixed name and no iss."""
    user = _event(source_ip="203.0.113.7", tenant_id=uuid4())
    system = _event(actor_type=ActorType.SYSTEM, actor_sub="worker", actor_iss=None)
    assert (user.actor_iss, system.actor_iss) == ("https://idp.example", None)
    assert user.id != system.id  # generated in code (no RETURNING)


@pytest.mark.parametrize(
    "overrides",
    [
        {"actor_sub": ""},
        {"actor_iss": None},
        {"actor_type": ActorType.SYSTEM, "actor_sub": "worker"},
        {"action": "Tenant.Disable"},
        {"action": "disable"},
        {"resource_type": ""},
        {"source_ip": "not-an-ip"},
    ],
)
def test_a_malformed_event_is_refused(overrides: dict[str, Any]) -> None:
    """Shapes the table's constraints would reject fail early (actor, action, ip)."""
    with pytest.raises(ValueError, match=r".+"):
        _event(**overrides)


@pytest.mark.parametrize(
    "overrides", [{"limit": 0}, {"limit": MAX_PAGE_SIZE + 1}, {"lag_seconds": -1}]
)
def test_a_bad_query_is_refused(overrides: dict[str, Any]) -> None:
    """The page size and the lag are bounded."""
    with pytest.raises(ValueError, match=r".+"):
        AuditQuery(**overrides)


def _record(**overrides: Any) -> AuditRecord:  # noqa: ANN401
    values: dict[str, Any] = {
        "id": uuid4(),
        "seq": 7,
        "occurred_at": datetime(2026, 11, 4, 12, 0, 0, 5, tzinfo=UTC),
        "recorded_at": datetime(2026, 11, 4, 14, 0, 1, tzinfo=timezone(timedelta(hours=2))),
        "tenant_id": uuid4(),
        "actor_type": ActorType.USER,
        "actor_sub": "sub-1",
        "actor_iss": "https://idp.example",
        "on_behalf_of": None,
        "source_ip": "2001:db8::1",
        "action": "credential.rotate",
        "resource_type": "credential",
        "resource_id": "c-1",
        "outcome": Outcome.DENIED,
        "before": {"state": "a"},
        "after": None,
        "request_id": "r-1",
        "trace_id": None,
        "schema_version": 1,
    }
    values.update(overrides)
    return AuditRecord(**values)


def test_ndjson_line_shape() -> None:  # inv. 12 (shape)
    """One compact JSON object per line, all columns in order, UTC timestamps, one newline."""
    record = _record()
    line = record.to_ndjson_line()
    assert line.endswith(b"\n")
    assert line.count(b"\n") == 1
    body = json.loads(line)
    assert list(body) == COLUMNS
    assert body["occurred_at"] == "2026-11-04T12:00:00.000005Z"
    assert body["recorded_at"] == "2026-11-04T12:00:01.000000Z"
    assert body["seq"] == 7
    assert body["id"] == str(record.id)
    assert body["tenant_id"] == str(record.tenant_id)
    assert (body["actor_type"], body["outcome"]) == ("user", "denied")
    assert (body["before"], body["after"]) == ({"state": "a"}, None)


def test_on_behalf_of_is_exported_as_a_json_object_or_null() -> None:
    """User decision: structured, so the NDJSON line nests it rather than quoting a string."""
    who = {"github_sender_id": 583231, "delivery_id": "d-1"}
    assert _record(on_behalf_of=who).to_json()["on_behalf_of"] == who
    assert json.loads(_record(on_behalf_of=who).to_ndjson_line())["on_behalf_of"] == who
    assert _record().to_json()["on_behalf_of"] is None


def test_a_null_tenant_is_exported_as_null() -> None:
    """An `authz.denied` row has no tenant."""
    assert _record(tenant_id=None).to_json()["tenant_id"] is None


def test_audit_settings_defaults_and_validation() -> None:
    """Defaults per ADR 0032; 0 means never purge; retention may not undercut the floor."""
    settings = make_settings([host()])
    assert (
        settings.audit_retention_days,
        settings.audit_retention_min_days,
        settings.audit_export_lag_seconds,
    ) == (365, 90, 30.0)
    assert make_settings([host()], audit_retention_days=0).audit_retention_days == 0
    assert make_settings([host()], audit_retention_days=90).audit_retention_days == 90
    with pytest.raises(ValidationError, match="AUDIT_RETENTION_DAYS"):
        make_settings([host()], audit_retention_days=30)
    with pytest.raises(ValidationError, match="audit_retention_min_days"):
        make_settings([host()], audit_retention_min_days=0)
    with pytest.raises(ValidationError, match="audit_export_lag_seconds"):
        make_settings([host()], audit_export_lag_seconds=0)


# -- explicit instance scope (review of NG6c): a missing tenant must never widen a read --------

NO_SESSION = cast(AsyncSession, None)  # the scope check raises before the session is touched


async def test_list_events_requires_an_explicit_scope() -> None:
    """No tenant and no `instance_scope=True` raises before any query."""
    with pytest.raises(ValueError, match="instance_scope"):
        await list_events(NO_SESSION, AuditQuery())
    with pytest.raises(ValueError, match="instance_scope"):
        await list_events(NO_SESSION, AuditQuery(), tenant_id=None)


async def test_list_events_rejects_instance_scope_with_a_tenant() -> None:
    """A tenant and `instance_scope=True` together is a contradiction."""
    with pytest.raises(ValueError, match="cannot be combined"):
        await list_events(NO_SESSION, AuditQuery(), tenant_id=uuid4(), instance_scope=True)


async def test_export_ndjson_enforces_the_same_scope_rules() -> None:
    """The export raises the same errors on its first iteration."""
    with pytest.raises(ValueError, match="instance_scope"):
        [line async for line in export_ndjson(NO_SESSION, AuditQuery(), lag_seconds=0)]
    with pytest.raises(ValueError, match="cannot be combined"):
        stream = export_ndjson(
            NO_SESSION, AuditQuery(), tenant_id=uuid4(), instance_scope=True, lag_seconds=0
        )
        [line async for line in stream]


@pytest.mark.parametrize("batch_size", [0, -1, 10001])
async def test_purge_expired_rejects_an_out_of_range_batch_size(batch_size: int) -> None:
    """The database's 1..10000 ceiling is checked client-side, before a transaction opens."""
    factory = cast(async_sessionmaker[AsyncSession], None)
    with pytest.raises(ValueError, match="batch_size"):
        await purge_expired(factory, retention_days=365, min_days=90, batch_size=batch_size)


class _FakeSession:
    """An `AsyncSession` stand-in: `async with factory() as s, s.begin()` and nothing else."""

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def begin(self) -> _FakeSession:
        return self


def _scripted_purge(monkeypatch: pytest.MonkeyPatch, counts: list[int]) -> list[int]:
    """Replace `purge_batch` with one that returns `counts` in turn; returns the call log."""
    calls: list[int] = []
    it = iter(counts)

    async def fake(_session: object, *, before: datetime, min_days: int, batch_size: int) -> int:
        calls.append(batch_size)
        return next(it)

    monkeypatch.setattr(store_module, "purge_batch", fake)
    return calls


def _fake_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], _FakeSession)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"retention_days": -1}, "negative"),
        ({"retention_days": 30}, "min_days"),
        ({"retention_days": 365, "max_batches": 0}, "max_batches"),
    ],
)
async def test_purge_expired_fails_closed_before_any_transaction(
    kwargs: dict[str, Any], message: str
) -> None:
    """A bad bound raises ValueError; the factory is `None`, so no transaction could open."""
    factory = cast(async_sessionmaker[AsyncSession], None)
    with pytest.raises(ValueError, match=message):
        await purge_expired(factory, min_days=90, **kwargs)


async def test_purge_expired_with_zero_retention_touches_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retention 0 never purges."""
    calls = _scripted_purge(monkeypatch, [])
    assert await purge_expired(_fake_factory(), retention_days=0, min_days=90) == 0
    assert calls == []


async def test_purge_expired_stops_at_a_short_batch_and_beats_per_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full batches continue, the first short one ends the run; `on_batch` runs per batch."""
    calls = _scripted_purge(monkeypatch, [5, 5, 2])
    beats: list[int] = []
    total = await purge_expired(
        _fake_factory(),
        retention_days=365,
        min_days=90,
        batch_size=5,
        on_batch=beats.append,
    )
    assert (total, calls, beats) == (12, [5, 5, 5], [5, 5, 2])


async def test_purge_expired_is_bounded_by_max_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Endless full batches stop at the cap; the next call continues."""
    calls = _scripted_purge(monkeypatch, [5] * 10)
    total = await purge_expired(
        _fake_factory(), retention_days=365, min_days=90, batch_size=5, max_batches=3
    )
    assert (total, len(calls)) == (15, 3)


async def test_purge_expired_stops_starting_batches_past_the_time_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the budget has elapsed no new batch starts; the next call continues."""
    calls = _scripted_purge(monkeypatch, [5] * 10)
    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0])  # start, then one reading per loop pass
    total = await purge_expired(
        _fake_factory(),
        retention_days=365,
        min_days=90,
        batch_size=5,
        budget_seconds=2.5,
        monotonic=lambda: next(ticks),
    )
    assert (total, len(calls)) == (10, 2)  # passes at t=1, 2 run; t=3 exceeds 2.5


def test_the_purge_settings_have_defaults_and_bounds() -> None:
    """The interval is positive, the batch size is the function's 1..10000, the cap at least 1."""
    s = make_settings([host()])
    assert (
        s.audit_purge_interval_seconds,
        s.audit_purge_batch_size,
        s.audit_purge_max_batches,
    ) == (
        3600.0,
        1000,
        100,
    )
    for bad in (
        {"audit_purge_interval_seconds": 0},
        {"audit_purge_batch_size": 0},
        {"audit_purge_batch_size": 10001},
        {"audit_purge_max_batches": 0},
        {"audit_purge_max_batches": 10001},
    ):
        with pytest.raises(ValidationError):
            make_settings([host()], **bad)
