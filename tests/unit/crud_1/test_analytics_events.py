"""Unit test: hero create/delete become hero.created/hero.deleted analytics events."""

import logging
from collections.abc import Callable, Mapping
from typing import Any

import pytest
from craftainer_crud.interfaces.base import CRUDInterface, InMemoryEventSink, OwnerScope
from craftainer_crud.repositories.memory import InMemoryRepository
from craftainer_observability.analytics import request_context
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud_1.analytics_events import AnalyticsEventSink
from app.crud_1.heroes.heroes_v2 import _hero_analytics_props, get_hero_crud
from app.models.hero import Hero as HeroModel
from app.views.hero_v2 import HeroV2, HeroV2Create

_EVENTS_LOGGER = "craftainer_observability.analytics.events"


class _RecordingSink:
    """An EventSink that records its calls, and can be told to fail."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail = fail

    async def publish(
        self, *, resource: str, record_id: int, action: str, snapshot: dict[str, Any]
    ) -> None:
        if self._fail:
            raise RuntimeError("broker down")
        self.calls.append(
            {"resource": resource, "record_id": record_id, "action": action, "snapshot": snapshot}
        )


def _props(_action: str, snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    return {"powers_count": len(snapshot["powers"])}


def _records(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """The `log` backend's records as plain dicts (event, url and prop fields)."""
    return [r.__dict__ for r in caplog.records if r.name == _EVENTS_LOGGER]


@pytest.fixture
def tracked(
    analytics_log: Callable[..., None], caplog: pytest.LogCaptureFixture
) -> pytest.LogCaptureFixture:
    """Switch to the log backend with `powers_count` allowlisted; capture its records."""
    analytics_log(prop_keys="powers_count")
    caplog.set_level(logging.INFO, logger=_EVENTS_LOGGER)
    return caplog


def _sink(inner: _RecordingSink) -> AnalyticsEventSink:
    return AnalyticsEventSink(inner, url="/crud/v1/heroes/v2", props_for=_props)


async def test_create_and_delete_are_forwarded_then_tracked(
    tracked: pytest.LogCaptureFixture,
) -> None:
    """The wrapped sink publishes first and unchanged; then hero.created/deleted are recorded."""
    inner = _RecordingSink()
    sink = _sink(inner)
    snapshot = {"name": "Spider-Man", "powers": ["Wall-crawling", "Spider-sense"]}

    await sink.publish(resource="hero", record_id=1, action="create", snapshot=snapshot)
    await sink.publish(resource="hero", record_id=1, action="delete", snapshot=snapshot)

    assert [c["action"] for c in inner.calls] == ["create", "delete"]
    assert inner.calls[0] == {
        "resource": "hero",
        "record_id": 1,
        "action": "create",
        "snapshot": snapshot,
    }
    records = _records(tracked)
    assert [r["analytics_event"] for r in records] == ["hero.created", "hero.deleted"]
    assert all(r["analytics_url"] == "/crud/v1/heroes/v2" for r in records)
    assert all(r["analytics_prop_powers_count"] == 2 for r in records)
    # Only the count is sent: no name, no id, no owner.
    assert all("Spider-Man" not in str(r) for r in records)


async def test_other_actions_are_forwarded_but_not_tracked(
    tracked: pytest.LogCaptureFixture,
) -> None:
    """update and restore still reach the wrapped sink; neither is a sampled domain event."""
    inner = _RecordingSink()
    sink = _sink(inner)
    for action in ("update", "restore"):
        await sink.publish(resource="hero", record_id=2, action=action, snapshot={"powers": ["x"]})
    assert [c["action"] for c in inner.calls] == ["update", "restore"]
    assert _records(tracked) == []


async def test_a_prop_key_off_the_allowlist_is_dropped_by_the_library(
    analytics_log: Callable[..., None], caplog: pytest.LogCaptureFixture
) -> None:
    """Without `powers_count` on ANALYTICS_PROP_KEYS the event is recorded with no props."""
    analytics_log()
    caplog.set_level(logging.INFO, logger=_EVENTS_LOGGER)
    await _sink(_RecordingSink()).publish(
        resource="hero", record_id=1, action="create", snapshot={"powers": ["x"]}
    )
    (record,) = _records(caplog)
    assert record["analytics_event"] == "hero.created"
    assert "analytics_prop_powers_count" not in record


async def test_a_failing_wrapped_sink_still_fails_and_records_nothing(
    tracked: pytest.LogCaptureFixture,
) -> None:
    """Analytics adds no error handling of its own: the inner failure propagates as before."""
    sink = _sink(_RecordingSink(fail=True))
    with pytest.raises(RuntimeError, match="broker down"):
        await sink.publish(resource="hero", record_id=1, action="create", snapshot={"powers": []})
    assert _records(tracked) == []


async def test_a_gpc_request_records_no_event_but_still_publishes(
    tracked: pytest.LogCaptureFixture,
) -> None:
    """Inside a GPC request context the mutation is published and not tracked."""
    inner = _RecordingSink()
    with request_context(gpc=True):
        await _sink(inner).publish(
            resource="hero", record_id=1, action="create", snapshot={"powers": ["x"]}
        )
    assert len(inner.calls) == 1
    assert _records(tracked) == []


async def test_an_opt_in_request_without_consent_records_no_event(
    analytics_log: Callable[..., None], caplog: pytest.LogCaptureFixture
) -> None:
    """ANALYTICS_CONSENT=opt-in: only a request with consent yields hero.created."""
    analytics_log(consent="opt-in", prop_keys="powers_count")
    caplog.set_level(logging.INFO, logger=_EVENTS_LOGGER)
    sink = _sink(_RecordingSink())
    with request_context(consent=False):
        await sink.publish(resource="hero", record_id=1, action="create", snapshot={"powers": []})
    assert _records(caplog) == []
    with request_context(consent=True):
        await sink.publish(resource="hero", record_id=1, action="create", snapshot={"powers": []})
    assert [r["analytics_event"] for r in _records(caplog)] == ["hero.created"]


def test_hero_analytics_props_counts_powers_and_tolerates_a_draft() -> None:
    """A draft hero has no powers yet: the count is 0, not an error."""
    assert _hero_analytics_props("create", {"powers": ["a", "b", "c"]}) == {"powers_count": 3}
    assert _hero_analytics_props("create", {"powers": None}) == {"powers_count": 0}
    assert _hero_analytics_props("delete", {}) == {"powers_count": 0}


async def test_the_real_crud_interface_emits_created_and_deleted(
    tracked: pytest.LogCaptureFixture,
) -> None:
    """A CRUDInterface whose events sink is the analytics wrapper emits both sample events."""
    crud: CRUDInterface[HeroV2, HeroModel] = CRUDInterface(
        schema=HeroV2,
        repository=InMemoryRepository(HeroModel),
        owner=OwnerScope("owner_id", "test-user", read_scoped=False),
        events=AnalyticsEventSink(
            InMemoryEventSink("hero"), url="/crud/v1/heroes/v2", props_for=_hero_analytics_props
        ),
        resource="hero",
    )
    created = await crud.create(HeroV2Create(name="Batman", powers=["Money", "Gadgets"]))
    assert await crud.delete(created.id) is True
    records = _records(tracked)
    assert [r["analytics_event"] for r in records] == ["hero.created", "hero.deleted"]
    assert all(r["analytics_prop_powers_count"] == 2 for r in records)
    assert all("Batman" not in str(r) for r in records)


def test_get_hero_crud_wires_the_analytics_sink() -> None:
    """The real dependency wraps Hero's event sink; the owner and resource are unchanged."""
    crud = get_hero_crud(AsyncSession(), {"sub": "u1"})
    assert isinstance(crud._events, AnalyticsEventSink)
    assert crud._resource == "hero"
