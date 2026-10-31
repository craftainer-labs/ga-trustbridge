"""Product-analytics events for a resource's CRUD mutations (ADR 0027).

`AnalyticsEventSink` wraps a resource's `EventSink` (the MQTT/in-memory one that feeds
`GET <prefix>/events`): the wrapped sink publishes first, exactly as before, then the
mutation is recorded through `craftainer_observability.analytics.track`. Two actions are
sampled as domain events, `create` as `<resource>.created` and `delete` as
`<resource>.deleted`; the rest (`update`, `restore`) are not tracked. Which request signals
apply (Global Privacy Control, DNT, consent) is decided by `app.analytics`'s middleware,
whose request context `track()` reads, so this module never looks at headers.

`track()` never raises and never blocks, and nothing here can fail a request that the wrapped
sink did not already fail: a prop key that is not on `ANALYTICS_PROP_KEYS` is dropped by the
library, and a record's own values are never sent, only the counts `props_for` derives.
"""

from collections.abc import Callable, Mapping
from typing import Any

from craftainer_crud.interfaces.base import EventSink
from craftainer_observability.analytics import track

_EVENT_SUFFIXES = {"create": "created", "delete": "deleted"}

PropsFor = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]


class AnalyticsEventSink:
    """An `EventSink` that forwards to `inner`, then records create/delete via `track()`."""

    def __init__(self, inner: EventSink, *, url: str, props_for: PropsFor) -> None:
        """Wrap `inner`; `url` is the path events are attributed to (query/fragment dropped).

        `props_for(action, snapshot)` returns the props for one event: scalars whose keys are
        on `ANALYTICS_PROP_KEYS`, and no personal data.
        """
        self._inner = inner
        self._url = url
        self._props_for = props_for

    async def publish(
        self, *, resource: str, record_id: int, action: str, snapshot: dict[str, Any]
    ) -> None:
        """Publish through the wrapped sink, then record the event if `action` is sampled."""
        await self._inner.publish(
            resource=resource, record_id=record_id, action=action, snapshot=snapshot
        )
        suffix = _EVENT_SUFFIXES.get(action)
        if suffix is not None:
            track(f"{resource}.{suffix}", self._props_for(action, snapshot), url=self._url)
