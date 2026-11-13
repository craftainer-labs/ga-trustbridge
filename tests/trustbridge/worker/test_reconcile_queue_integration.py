"""The reconcile queue's Lua scripts against the stack's Valkey (NG5r-a).

The unit tests run a Python mirror of each script; this checks the real ones on a throwaway
key set: the scripts are the same text the queue sends, only the key names differ.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from redis.asyncio import from_url as redis_from_url

from app.config import get_settings
from trustbridge.worker import reconcile_queue
from trustbridge.worker.reconcile_queue import ReconcileQueue

pytestmark = pytest.mark.integration

TTL = 300.0
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


@asynccontextmanager
async def keys() -> AsyncIterator[tuple[Redis, dict[str, str]]]:
    """Yield a client whose queue keys are unique to the test, and removed after it."""
    client = redis_from_url(get_settings().redis_url, decode_responses=True)
    suffix = uuid4().hex
    names = {
        "RECONCILE_SET": f"tb:reconcile:test:{suffix}",
        "INFLIGHT_SET": f"tb:reconcile:inflight:test:{suffix}",
        "CLAIMS_HASH": f"tb:reconcile:claims:test:{suffix}",
    }
    originals = {name: getattr(reconcile_queue, name) for name in names}
    for name, value in names.items():
        setattr(reconcile_queue, name, value)
    try:
        yield client, names
    finally:
        for name, value in originals.items():
            setattr(reconcile_queue, name, value)
        await client.delete(*names.values())
        await client.aclose()


async def test_claim_ack_release_and_reclaim_run_as_scripts() -> None:
    """The four scripts keep the sets and the claim hash consistent, token and listing checked."""
    async with keys() as (client, names):
        queue = ReconcileQueue(client)
        first, second = str(uuid4()), str(uuid4())
        await client.sadd(names["RECONCILE_SET"], first, second)
        claims = await queue.claim(NOW, 10)
        assert {str(c.tenant_id) for c in claims} == {first, second}
        assert (await queue.pending(), await queue.inflight()) == (0, 2)
        one, other = claims
        assert await queue.ack(one) is True
        assert await queue.ack(one) is False
        assert await queue.release(other) is True
        assert (await queue.pending(), await queue.inflight()) == (1, 0)
        assert await client.hlen(names["CLAIMS_HASH"]) == 0
        (again,) = await queue.claim(NOW, 10)
        assert await queue.reclaim(NOW + timedelta(seconds=TTL - 1), TTL) == 0
        assert await queue.reclaim(NOW + timedelta(seconds=TTL + 1), TTL) == 1
        assert await queue.ack(again) is False  # the slow holder finds out
        assert (await queue.pending(), await queue.inflight()) == (1, 0)


async def test_a_stale_token_changes_nothing_and_a_non_canonical_member_is_requeued() -> None:
    """The token compare of ack and release, and canonical re-queueing."""
    async with keys() as (client, names):
        queue = ReconcileQueue(client)
        tenant = uuid4()
        await client.sadd(names["RECONCILE_SET"], str(tenant).upper())
        assert await queue.claim(NOW, 10) == []  # re-queued under its canonical text
        assert await client.smembers(names["RECONCILE_SET"]) == {str(tenant)}
        (stale,) = await queue.claim(NOW, 10)
        later = NOW + timedelta(seconds=TTL + 1)
        assert await queue.reclaim(later, TTL) == 1
        (fresh,) = await queue.claim(later, 10)
        assert await queue.ack(stale) is False
        assert await queue.release(stale) is False
        assert await client.smembers(names["INFLIGHT_SET"]) == {str(tenant)}
        assert await queue.ack(fresh) is True
        assert await client.hlen(names["CLAIMS_HASH"]) == 0


async def test_an_unstamped_member_is_stamped_then_reclaimed() -> None:
    """An inflight member with no (or an unreadable) entry is stamped, then reclaimed a TTL on."""
    async with keys() as (client, names):
        queue = ReconcileQueue(client)
        bare, garbled = str(uuid4()), str(uuid4())
        await client.sadd(names["INFLIGHT_SET"], bare, garbled)
        await client.hset(names["CLAIMS_HASH"], garbled, "garbage")
        assert await queue.reclaim(NOW, TTL) == 0
        assert await client.hlen(names["CLAIMS_HASH"]) == 2
        assert await queue.reclaim(NOW + timedelta(seconds=TTL + 1), TTL) == 2
        assert await client.smembers(names["RECONCILE_SET"]) == {bare, garbled}
        assert await client.hlen(names["CLAIMS_HASH"]) == 0
