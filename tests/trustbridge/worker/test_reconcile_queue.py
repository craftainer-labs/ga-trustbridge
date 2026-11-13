"""The tenant reconcile requests: claim, ack, reclaim and the reconciler's drain (NG5r-a)."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from tests.trustbridge.fakes import Clock, FakeRedis
from tests.trustbridge.helpers import as_redis, make_settings
from tests.trustbridge.worker.harness import Harness
from trustbridge.worker.health import Heartbeat
from trustbridge.worker.ports import RECONCILE_SET, ReconcileRequests
from trustbridge.worker.reconcile_queue import CLAIMS_HASH, INFLIGHT_SET, ReconcileQueue
from trustbridge.worker.reconciler import Reconciler, ReconcilerDeps

TTL = 300.0


def _queue() -> tuple[FakeRedis, ReconcileQueue, Clock]:
    valkey = FakeRedis()
    return valkey, ReconcileQueue(as_redis(valkey)), Clock()


async def _request(valkey: FakeRedis, *tenants: UUID) -> None:
    requests = ReconcileRequests(as_redis(valkey))
    for tenant in tenants:
        await requests.schedule(tenant)


async def test_a_claim_moves_the_request_to_inflight_and_an_ack_removes_it() -> None:
    """SMOVE to inflight, a stamp in the claims hash, then SREM and HDEL."""
    valkey, queue, clock = _queue()
    tenant = uuid4()
    await _request(valkey, tenant)
    (claim,) = await queue.claim(clock(), 10)
    assert claim.tenant_id == tenant
    assert valkey.sets == {INFLIGHT_SET: {str(tenant)}}
    assert (await queue.pending(), await queue.inflight()) == (0, 1)
    assert await queue.ack(claim) is True
    assert valkey.sets == {}
    assert valkey.hashes == {}
    assert await queue.ack(claim) is False  # the second ack of the same claim finds nothing


async def test_two_replicas_claiming_at_once_never_share_a_request() -> None:
    """SMOVE is atomic: every request is claimed by exactly one of two concurrent claimers."""
    valkey, first, clock = _queue()
    second = ReconcileQueue(as_redis(valkey))
    tenants = {uuid4() for _ in range(20)}
    await _request(valkey, *tenants)
    a, b = await asyncio.gather(first.claim(clock(), 20), second.claim(clock(), 20))
    got_a, got_b = {c.tenant_id for c in a}, {c.tenant_id for c in b}
    assert not got_a & got_b
    assert got_a | got_b == tenants


async def test_the_claim_limit_bounds_one_run() -> None:
    """At most `limit` requests are claimed; the rest stay pending."""
    valkey, queue, clock = _queue()
    await _request(valkey, *(uuid4() for _ in range(5)))
    assert len(await queue.claim(clock(), 2)) == 2
    assert (await queue.pending(), await queue.inflight()) == (3, 2)


async def test_a_request_made_while_claimed_survives_the_ack() -> None:
    """The api adds the tenant again to the pending set; the ack only touches inflight."""
    valkey, queue, clock = _queue()
    tenant = uuid4()
    await _request(valkey, tenant)
    (claim,) = await queue.claim(clock(), 1)
    await _request(valkey, tenant)
    assert await queue.ack(claim) is True
    assert valkey.sets == {RECONCILE_SET: {str(tenant)}}


async def test_a_crash_between_claim_and_ack_is_reclaimed_after_the_ttl() -> None:
    """The member stays inflight (never lost); a claim younger than the TTL is not touched."""
    valkey, queue, clock = _queue()
    tenant = uuid4()
    await _request(valkey, tenant)
    (stale,) = await queue.claim(clock(), 1)
    clock.advance(seconds=TTL - 1)
    assert await queue.reclaim(clock(), TTL) == 0
    assert valkey.sets == {INFLIGHT_SET: {str(tenant)}}
    clock.advance(seconds=2)
    assert await queue.reclaim(clock(), TTL) == 1
    assert valkey.sets == {RECONCILE_SET: {str(tenant)}}
    assert valkey.hashes == {}
    # the slow holder finds out at its ack, and the new holder is not disturbed
    (fresh,) = await queue.claim(clock(), 1)
    assert await queue.ack(stale) is False
    assert await queue.release(stale) is False
    assert valkey.sets == {INFLIGHT_SET: {str(tenant)}}
    assert await queue.ack(fresh) is True


async def test_a_crash_between_the_move_and_the_stamp_is_stamped_then_reclaimed() -> None:
    """A member with no claim entry is not reclaimed at once (its claimer may be slow)."""
    valkey, queue, clock = _queue()
    tenant = uuid4()
    valkey.sets[INFLIGHT_SET] = {str(tenant)}
    assert await queue.reclaim(clock(), TTL) == 0
    assert CLAIMS_HASH in valkey.hashes
    clock.advance(seconds=TTL + 1)
    assert await queue.reclaim(clock(), TTL) == 1
    assert valkey.sets == {RECONCILE_SET: {str(tenant)}}


async def test_an_unreadable_claim_entry_is_restamped_not_trusted() -> None:
    """A garbage entry is treated as no entry (it is never reclaimed on its own say-so)."""
    valkey, queue, clock = _queue()
    tenant = uuid4()
    valkey.sets[INFLIGHT_SET] = {str(tenant)}
    valkey.hashes[CLAIMS_HASH] = {str(tenant): "garbage"}
    assert await queue.reclaim(clock(), TTL) == 0
    assert valkey.hashes[CLAIMS_HASH][str(tenant)] == "garbage"  # hsetnx keeps the entry


async def test_a_claim_taken_again_since_the_listing_is_not_reclaimed() -> None:
    """The entry is compared again, in the same step as the delete and the move."""
    valkey, queue, clock = _queue()
    tenant = uuid4()
    await _request(valkey, tenant)
    (claim,) = await queue.claim(clock(), 1)
    clock.advance(seconds=TTL + 1)
    real_hgetall = valkey.hgetall

    async def renewed(name: str) -> dict[str, str]:
        listed = await real_hgetall(name)
        valkey.hashes[CLAIMS_HASH][str(tenant)] = f"{clock().timestamp():.3f}|other"
        return listed

    valkey.hgetall = renewed  # type: ignore[method-assign]
    assert await queue.reclaim(clock(), TTL) == 0
    assert valkey.sets == {INFLIGHT_SET: {str(tenant)}}
    assert claim.token not in valkey.hashes[CLAIMS_HASH][str(tenant)]


async def test_release_puts_the_request_back_to_pending() -> None:
    """A claim that was not acted on goes back and can be claimed again."""
    valkey, queue, clock = _queue()
    tenant = uuid4()
    await _request(valkey, tenant)
    (claim,) = await queue.claim(clock(), 1)
    assert await queue.release(claim) is True
    assert valkey.sets == {RECONCILE_SET: {str(tenant)}}
    assert valkey.hashes == {}
    assert len(await queue.claim(clock(), 1)) == 1


async def test_a_malformed_member_is_dropped_with_a_log(caplog: pytest.LogCaptureFixture) -> None:
    """Nothing the api writes; it must not wedge the drain."""
    valkey, queue, clock = _queue()
    valkey.sets[RECONCILE_SET] = {"not-a-uuid"}
    with caplog.at_level(logging.WARNING):
        assert await queue.claim(clock(), 5) == []
    assert valkey.sets == {}
    assert "malformed reconcile request" in caplog.text


async def test_a_request_claimed_by_someone_else_is_skipped() -> None:
    """The member left the set between the listing and the SMOVE."""
    valkey, queue, clock = _queue()
    await _request(valkey, uuid4())
    real = valkey.srandmember

    async def stale_listing(name: str, number: int) -> list[str]:
        members = await real(name, number)
        valkey.sets.pop(name, None)  # another replica took them
        return members

    valkey.srandmember = stale_listing  # type: ignore[method-assign]
    assert await queue.claim(clock(), 5) == []
    assert valkey.sets == {}


# ---- the reconciler's duty -------------------------------------------------------------


class Refusals:
    """A `TenantRefusals` over a dict; an unlisted tenant may be reconciled."""

    def __init__(self, **by_tenant: str) -> None:
        """Map tenant id strings to a refusal."""
        self.by_tenant = by_tenant
        self.asked: list[UUID] = []
        self.fail = False

    async def refusal_of(self, tenant_id: UUID) -> str | None:
        """The refusal of the tenant."""
        self.asked.append(tenant_id)
        if self.fail:
            raise ConnectionError("database down")
        return self.by_tenant.get(str(tenant_id))


class Body:
    """A `TenantReconciler` that records its calls and can run a hook or fail."""

    def __init__(self) -> None:
        """Start with no calls."""
        self.calls: list[UUID] = []
        self.error: Exception | None = None
        self.hook: Callable[[], Awaitable[None]] | None = None

    async def reconcile(self, tenant_id: UUID) -> None:
        """Record, then maybe fail."""
        self.calls.append(tenant_id)
        if self.hook is not None:
            await self.hook()
        if self.error is not None:
            raise self.error


def _duty(
    h: Harness,
    refusals: Refusals | None,
    body: Body | None,
    *,
    queue: ReconcileQueue | None = None,
    beat: Heartbeat | None = None,
) -> Reconciler:
    return Reconciler(
        h.settings,
        ReconcilerDeps(
            store=h.store,
            queue=h.queue,
            relay=h.relay,
            hosts=h.hosts,
            leases=h.leases,
            deliveries=None,
            clock=h.clock,
            heartbeat=beat,
            reconcile_requests=queue if queue is not None else ReconcileQueue(as_redis(h.valkey)),
            refusals=refusals,
            tenant_reconciler=body,
        ),
    )


@pytest.mark.parametrize("refusal", ["gone", "deleting", "disabled"])
async def test_a_request_for_a_refused_tenant_is_acked_and_dropped_with_a_log(
    refusal: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Invariant 19 and tenant deletion (ADR 0034): nothing runs, the request is gone."""
    h = await Harness({}, tenant_reconcile_enabled=True).start()
    tenant, body = uuid4(), Body()
    await _request(h.valkey, tenant)
    with caplog.at_level(logging.INFO):
        assert await _duty(h, Refusals(**{str(tenant): refusal}), body).tenant_reconcile() == 1
    assert body.calls == []
    assert h.valkey.sets == {}
    assert h.valkey.hashes == {}
    assert f"dropped a reconcile request: tenant is {refusal}" in caplog.text


async def test_the_refusal_is_dropped_even_while_the_body_is_off() -> None:
    """A disabled tenant's request is dropped whatever the flag says."""
    h = await Harness({}).start()
    tenant = uuid4()
    await _request(h.valkey, tenant)
    assert await _duty(h, Refusals(**{str(tenant): "disabled"}), None).tenant_reconcile() == 1
    assert h.valkey.sets == {}


@pytest.mark.parametrize(("enabled", "has_body"), [(False, True), (False, False), (True, False)])
async def test_without_an_enabled_body_a_request_stays_pending(
    enabled: bool, has_body: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """A request answered 202 is never consumed by a duty that does nothing."""
    h = await Harness({}, tenant_reconcile_enabled=enabled).start()
    tenant, body = uuid4(), Body()
    await _request(h.valkey, tenant)
    recon = _duty(h, Refusals(), body if has_body else None)
    with caplog.at_level(logging.INFO):
        assert await recon.tenant_reconcile() == 0
        assert await recon.tenant_reconcile() == 0  # and again: nothing is lost by polling
    assert body.calls == []
    assert h.valkey.sets == {RECONCILE_SET: {str(tenant)}}
    assert h.valkey.hashes == {}
    assert "left pending: no reconcile body" in caplog.text


async def test_an_enabled_body_runs_once_per_request_and_the_request_is_acked() -> None:
    """The body runs after the check, the claim is acknowledged, a repeat request is one."""
    h = await Harness({}, tenant_reconcile_enabled=True).start()
    tenant, body = uuid4(), Body()
    await _request(h.valkey, tenant, tenant)
    beat = Heartbeat()
    beat.register("tenant-reconcile", 60)
    assert await _duty(h, Refusals(), body, beat=beat).tenant_reconcile() == 1
    assert body.calls == [tenant]
    assert h.valkey.sets == {}


async def test_a_failing_body_leaves_the_claim_inflight_and_it_is_retried_after_the_ttl(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A crash or an error never loses the request; the retry waits the claim TTL."""
    h = await Harness({}, tenant_reconcile_enabled=True).start()
    tenant, body = uuid4(), Body()
    body.error = RuntimeError("platform down")
    await _request(h.valkey, tenant)
    recon = _duty(h, Refusals(), body)
    with caplog.at_level(logging.ERROR):
        assert await recon.tenant_reconcile() == 0
    assert "tenant reconcile failed" in caplog.text
    assert h.valkey.sets == {INFLIGHT_SET: {str(tenant)}}
    h.advance(TTL - 1)
    assert await recon.tenant_reconcile() == 0
    assert len(body.calls) == 1  # not yet reclaimed
    body.error = None
    h.advance(2)
    assert await recon.tenant_reconcile() == 1
    assert body.calls == [tenant, tenant]
    assert h.valkey.sets == {}


async def test_a_database_error_on_the_check_leaves_the_claim_inflight() -> None:
    """The standing could not be read: nothing runs and nothing is dropped."""
    h = await Harness({}, tenant_reconcile_enabled=True).start()
    tenant, body, refusals = uuid4(), Body(), Refusals()
    refusals.fail = True
    await _request(h.valkey, tenant)
    assert await _duty(h, refusals, body).tenant_reconcile() == 0
    assert body.calls == []
    assert h.valkey.sets == {INFLIGHT_SET: {str(tenant)}}


async def test_a_slow_body_whose_claim_was_reclaimed_is_detected_at_its_ack(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Another replica reclaims and re-runs; the first finds out and does not remove the claim."""
    h = await Harness({}, tenant_reconcile_enabled=True).start()
    tenant = uuid4()
    first, second = Body(), Body()
    await _request(h.valkey, tenant)
    other = _duty(h, Refusals(), second)

    async def take_over() -> None:
        h.advance(TTL + 1)
        assert await other.tenant_reconcile() == 1  # reclaims, claims, runs and acks

    first.hook = take_over
    with caplog.at_level(logging.WARNING):
        assert await _duty(h, Refusals(), first).tenant_reconcile() == 1
    assert (first.calls, second.calls) == ([tenant], [tenant])  # processed twice, detected
    assert "reclaimed while it ran" in caplog.text
    assert h.valkey.sets == {}


async def test_without_a_queue_or_a_database_the_duty_does_nothing() -> None:
    """Mock mode composes neither."""
    h = await Harness({}).start()
    assert await _duty(h, None, None).tenant_reconcile() == 0
    recon = Reconciler(
        h.settings,
        ReconcilerDeps(
            store=h.store,
            queue=h.queue,
            relay=h.relay,
            hosts=h.hosts,
            leases=h.leases,
            deliveries=None,
            clock=h.clock,
        ),
    )
    assert await recon.tenant_reconcile() == 0


def test_the_claim_ttl_must_not_be_shorter_than_the_lease_ttl() -> None:
    """A reconcile that holds a scope lease must not be reclaimed under it."""
    with pytest.raises(ValidationError, match="TENANT_RECONCILE_CLAIM_TTL_SECONDS"):
        make_settings(lease_ttl_seconds=120, tenant_reconcile_claim_ttl_seconds=60)
    assert make_settings(tenant_reconcile_enabled=True).tenant_reconcile_enabled is True
    assert timedelta(seconds=make_settings().tenant_reconcile_claim_ttl_seconds) > timedelta(0)


async def test_a_request_acked_between_the_listing_and_the_reclaim_is_not_counted() -> None:
    """The holder acked after the listing: the entry is gone, so nothing is moved."""
    valkey, queue, clock = _queue()
    await _request(valkey, uuid4())
    (claim,) = await queue.claim(clock(), 1)
    clock.advance(seconds=TTL + 1)
    real_hgetall = valkey.hgetall

    async def holder_acks(name: str) -> dict[str, str]:
        listed = await real_hgetall(name)
        assert await queue.ack(claim) is True
        return listed

    valkey.hgetall = holder_acks  # type: ignore[method-assign]
    assert await queue.reclaim(clock(), TTL) == 0
    assert valkey.sets == {}


async def test_a_stale_holder_cannot_undo_a_newer_claim() -> None:
    """Ack and release check the token in the same step as they change the sets."""
    valkey, queue, clock = _queue()
    tenant = uuid4()
    await _request(valkey, tenant)
    (stale,) = await queue.claim(clock(), 1)
    clock.advance(seconds=TTL + 1)
    assert await queue.reclaim(clock(), TTL) == 1
    (fresh,) = await queue.claim(clock(), 1)
    assert await queue.ack(stale) is False
    assert await queue.release(stale) is False
    assert valkey.sets == {INFLIGHT_SET: {str(tenant)}}
    assert fresh.token in valkey.hashes[CLAIMS_HASH][str(tenant)]


async def test_a_non_canonical_member_is_requeued_canonically() -> None:
    """`UUID()` accepts upper case; a claim stored under it could never be acknowledged."""
    valkey, queue, clock = _queue()
    tenant = uuid4()
    valkey.sets[RECONCILE_SET] = {str(tenant).upper()}
    assert await queue.claim(clock(), 5) == []
    assert valkey.sets == {RECONCILE_SET: {str(tenant)}}
    (claim,) = await queue.claim(clock(), 5)
    assert await queue.ack(claim) is True


async def test_a_member_acked_before_it_is_stamped_leaves_no_entry() -> None:
    """The stamp of an unstamped member is made only while the member is still inflight."""
    valkey, queue, clock = _queue()
    tenant = uuid4()
    valkey.sets[INFLIGHT_SET] = {str(tenant)}
    real_smembers = valkey.smembers

    async def acked_after_listing(name: str) -> set[str]:
        listed = await real_smembers(name)
        valkey.sets.pop(INFLIGHT_SET, None)
        return listed

    valkey.smembers = acked_after_listing  # type: ignore[method-assign]
    assert await queue.reclaim(clock(), TTL) == 0
    assert valkey.hashes == {}


async def test_claims_not_started_within_half_the_ttl_go_back_to_pending() -> None:
    """A long batch never lets a queued claim expire before its body starts."""
    h = await Harness({}, tenant_reconcile_enabled=True).start()
    first, second = uuid4(), uuid4()
    body = Body()

    async def slow() -> None:
        h.advance(TTL)  # the first body outlasts half the claim TTL

    body.hook = slow
    await _request(h.valkey, first, second)
    assert await _duty(h, Refusals(), body).tenant_reconcile() == 1
    assert len(body.calls) == 1
    left = {str(first), str(second)} - {str(body.calls[0])}
    assert h.valkey.sets == {RECONCILE_SET: left}
    assert h.valkey.hashes == {}


async def test_run_logs_why_requests_stay_pending(caplog: pytest.LogCaptureFixture) -> None:
    """With the queue and the check composed but no body, the startup says so."""
    h = await Harness({}).start()
    stop = asyncio.Event()
    stop.set()
    with caplog.at_level(logging.INFO):
        await _duty(h, Refusals(), None).run(stop)
    assert "stay pending: no reconcile body is enabled" in caplog.text


async def test_run_is_quiet_when_a_body_is_composed_and_enabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No startup line about pending requests when the body will run."""
    h = await Harness({}, tenant_reconcile_enabled=True).start()
    stop = asyncio.Event()
    stop.set()
    with caplog.at_level(logging.INFO):
        await _duty(h, Refusals(), Body()).run(stop)
    assert "tenant reconcile" not in caplog.text
