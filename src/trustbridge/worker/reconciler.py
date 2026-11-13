"""The reconciler process: relay ticks, re-relay sweep, delivery reconciliation, retention, purge,
tenant deletion, the tenant reconcile requests.

GitHub never redelivers on its own (ADR 0028 item 4). The delivery reconciler lists failed
hook deliveries per `ready` host with the App JWT endpoints (`GET /app/hook/deliveries`,
`POST /app/hook/deliveries/{id}/attempts`) through the `HookDeliveryClient` Protocol (NG4
implements it) and redelivers only those whose `X-GitHub-Delivery` guid has **no stored
row** (invariant 8); a stored row is durable and owned by the relay and the worker.

Singleton duties (delivery reconciliation per host, DB retention, audit purge, tenant deletion,
deletion record purge) run under a
lease row so replicas do not duplicate them (EB-6); the relay tick, the re-relay sweep and
stream trimming are idempotent and run on every replica.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.audit.store import purge_expired
from trustbridge.deletion.ports import DeletionDriver, RecordPurger
from trustbridge.github.availability import HostMonitor
from trustbridge.settings import GitHubHostConfig, TrustbridgeSettings
from trustbridge.worker import metrics
from trustbridge.worker.deliveries import DeliveryKey, DeliveryStore
from trustbridge.worker.health import Heartbeat, stale_after
from trustbridge.worker.lease import LeaseBusy, LeaseManager
from trustbridge.worker.models import NIL_TENANT
from trustbridge.worker.queue import StreamQueue
from trustbridge.worker.reconcile_queue import Claim, ClaimLostError, Extend, ReconcileQueue
from trustbridge.worker.relay import Clock, Relay, utcnow

logger = logging.getLogger(__name__)
CORE_ADAPTER = "_core"
HTTP_OK_LOW, HTTP_OK_HIGH = 200, 300


@dataclass(frozen=True, slots=True)
class HookDelivery:
    """One attempt listed by `GET /app/hook/deliveries`."""

    id: int  # GitHub's numeric delivery id (for the redelivery endpoint)
    guid: str  # the `X-GitHub-Delivery` header value (our `delivery_id`)
    status_code: int
    delivered_at: datetime
    redelivery: bool = False


class HookDeliveryClient(Protocol):
    """GitHub's hook-delivery endpoints for one host's App (implemented by NG4)."""

    def list_deliveries(
        self, host: GitHubHostConfig, *, since: datetime
    ) -> AsyncIterator[HookDelivery]:
        """Attempts newest first, until `delivered_at < since` (follows pagination)."""
        ...

    async def redeliver(self, host: GitHubHostConfig, hook_delivery_id: int) -> None:
        """`POST /app/hook/deliveries/{id}/attempts`."""
        ...


class DeliveryReconciler:
    """Redelivers failed GitHub deliveries that never reached `webhook_deliveries`."""

    def __init__(
        self,
        store: DeliveryStore,
        client: HookDeliveryClient,
        *,
        lookback: timedelta,
        max_per_run: int,
        clock: Clock = utcnow,
    ) -> None:
        """Bind the outbox, the GitHub client and the per-run bounds."""
        self._store = store
        self._client = client
        self._lookback = lookback
        self._max = max_per_run
        self._clock = clock

    async def reconcile_host(self, host: GitHubHostConfig) -> int:
        """Redeliver this host's failed deliveries that have no stored row; returns the count."""
        since = self._clock() - self._lookback
        latest: dict[str, HookDelivery] = {}
        async for item in self._client.list_deliveries(host, since=since):
            latest.setdefault(item.guid, item)  # newest first: the first seen is the latest
        failed = [d for d in latest.values() if not HTTP_OK_LOW <= d.status_code < HTTP_OK_HIGH]
        if not failed:
            return 0
        stored = await self._store.existing_keys([DeliveryKey(host.name, d.guid) for d in failed])
        redelivered = 0
        for item in failed:
            key = DeliveryKey(host.name, item.guid)
            if key in stored or redelivered >= self._max:
                continue
            if await self._store.existing_keys([key]):  # re-check just before the call
                continue
            await self._client.redeliver(host, item.id)
            redelivered += 1
        if redelivered:
            logger.info(
                "redelivered failed deliveries", extra={"host": host.name, "count": redelivered}
            )
        return redelivered


class TenantRefusals(Protocol):
    """Whether a tenant may be reconciled at all (`SqlTenantGate`, ADR 0033 invariant 19)."""

    async def refusal_of(self, tenant_id: UUID) -> str | None:
        """`gone`, `deleting` or `disabled`; None when a reconcile may run."""
        ...


class TenantReconciler(Protocol):
    """The tenant reconcile body (`CredentialReconciler`). Idempotent: a request can run twice."""

    async def reconcile(self, tenant_id: UUID, extend: Extend) -> None:
        """Bring the tenant's credentials to the desired state.

        `extend` re-stamps the running claim (and beats the loop's heartbeat); the body calls it
        before each write step, and it raises `ClaimLostError` when the claim was reclaimed.
        """
        ...


Job = Callable[[], Awaitable[object]]


async def periodic(
    name: str,
    interval: float,
    job: Job,
    stop: asyncio.Event,
    beat: Callable[[str], None] | None = None,
) -> None:
    """Run `job` now and then every `interval` seconds until `stop`; errors are logged.

    `beat(name)` is called when a run starts and when it ends (even if it failed): the gap
    between two calls is what the liveness check measures.
    """
    while not stop.is_set():
        if beat is not None:
            beat(name)
        try:
            await job()
        except Exception:
            logger.exception("reconciler job failed", extra={"job": name})
        if beat is not None:
            beat(name)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)


@dataclass(slots=True)
class ReconcilerDeps:
    """Collaborators of a `Reconciler`."""

    store: DeliveryStore
    queue: StreamQueue
    relay: Relay
    hosts: HostMonitor
    leases: LeaseManager
    deliveries: HookDeliveryClient | None
    clock: Clock = utcnow
    heartbeat: Heartbeat | None = None
    audit_sessions: async_sessionmaker[AsyncSession] | None = None  # None: mock mode, no purge
    deletion: DeletionDriver | None = None  # None: no deletion engine composed, no duty
    record_purger: RecordPurger | None = None  # None: not composed (NG6h-7), no purge duty
    reconcile_requests: ReconcileQueue | None = None  # None: no Valkey (mock mode), no duty
    refusals: TenantRefusals | None = None  # None: no database, no duty
    tenant_reconciler: TenantReconciler | None = None  # None: mock mode, no body


class Reconciler:
    """Runs the periodic jobs of the reconciler process."""

    def __init__(self, settings: TrustbridgeSettings, deps: ReconcilerDeps) -> None:
        """Bind settings and collaborators."""
        self._s = settings
        self._d = deps

    def _beat(self, job: str) -> None:
        """Record progress inside a job that works through several steps."""
        if self._d.heartbeat is not None:
            self._d.heartbeat.beat(job)

    async def relay_tick(self) -> int:
        """Relay rows never relayed and due retries."""
        return await self._d.relay.tick()

    async def resweep(self) -> int:
        """Re-add every pending row (repairs a stream that lost data)."""
        return await self._d.relay.resweep(self._s.relay_resweep_min_age_seconds)

    async def reconcile_deliveries(self) -> int:
        """Per ready host, under its lease, redeliver failed deliveries that have no row."""
        client = self._d.deliveries
        if client is None:
            return 0
        reconciler = DeliveryReconciler(
            self._d.store,
            client,
            lookback=timedelta(hours=self._s.delivery_reconcile_lookback_hours),
            max_per_run=self._s.delivery_reconcile_max_per_run,
            clock=self._d.clock,
        )
        total = 0
        for name in self._d.hosts.names():
            if not self._d.hosts.is_ready(name):
                continue
            host = self._d.hosts.config(name)
            self._beat("deliveries")  # one host is a unit of progress
            try:
                async with self._d.leases.hold(
                    NIL_TENANT, CORE_ADAPTER, f"reconcile-deliveries:{name}"
                ):
                    total += await reconciler.reconcile_host(host)
            except LeaseBusy:
                continue
            except Exception:
                logger.exception("delivery reconciliation failed", extra={"host": name})
        return total

    async def retention(self) -> None:
        """Purge bodies and expired ids (singleton under a lease)."""
        now = self._d.clock()
        try:
            async with self._d.leases.hold(NIL_TENANT, CORE_ADAPTER, "retention"):
                self._beat("retention")
                purged = await self._d.store.purge_bodies(
                    now - timedelta(days=self._s.body_retention_processed_days),
                    now - timedelta(days=self._s.body_retention_dead_days),
                )
                self._beat("retention")
                deleted = await self._d.store.delete_expired(
                    now - timedelta(days=self._s.delivery_id_retention_days)
                )
                logger.info("retention", extra={"purged_bodies": purged, "deleted_rows": deleted})
        except LeaseBusy:
            return

    async def audit_purge(self) -> int:
        """Delete audit events past `AUDIT_RETENTION_DAYS` (singleton under a lease).

        Retention 0 never purges. Bounded: batches of `AUDIT_PURGE_BATCH_SIZE`, at most
        `AUDIT_PURGE_MAX_BATCHES` per run, a heartbeat per batch. The purge's own summary event
        is written by `tb_audit_purge` (ADR 0032), so this job writes no audit row.
        """
        s = self._s
        sessions = self._d.audit_sessions
        if sessions is None or s.audit_retention_days == 0:
            return 0
        deleted = 0
        try:
            async with self._d.leases.hold(NIL_TENANT, CORE_ADAPTER, "audit-purge"):
                self._beat("audit-purge")

                def account(batch: int) -> None:
                    nonlocal deleted
                    deleted += batch
                    self._beat("audit-purge")
                    metrics.audit_purged.add(batch)
                    logger.info("audit purge batch", extra={"deleted_events": batch})

                await purge_expired(
                    sessions,
                    retention_days=s.audit_retention_days,
                    min_days=s.audit_retention_min_days,
                    batch_size=s.audit_purge_batch_size,
                    max_batches=s.audit_purge_max_batches,
                    on_batch=account,
                    budget_seconds=s.lease_ttl_seconds / 2,
                    now=self._d.clock(),
                )
        except LeaseBusy:
            return 0
        logger.info("audit purge", extra={"deleted_events": deleted})
        return deleted

    async def tenant_deletion(self) -> int:
        """Run tenant deletions through the `DeletionDriver` (singleton under a lease).

        Claims at most `DELETION_BATCH_SIZE` deletions per poll and beats once per pass, so a
        long run (bounded by the engine's own budget) never looks stalled. A failure of the
        record store that escapes `run_once` is logged and ends this poll only: the loop stays
        alive, the deletion's own lease lapses and the next poll claims it again (ADR 0034).
        Returns the number of deletions run.
        """
        driver = self._d.deletion
        if driver is None:
            return 0
        ran = 0
        try:
            async with self._d.leases.hold(NIL_TENANT, CORE_ADAPTER, "tenant-deletion"):
                for _ in range(self._s.deletion_batch_size):
                    self._beat("tenant-deletion")
                    try:
                        claimed = await driver.run_once()
                    except Exception:
                        logger.exception("tenant deletion pass failed")
                        break
                    if not claimed:
                        break
                    ran += 1
        except LeaseBusy:
            return 0
        self._beat("tenant-deletion")
        if ran:
            logger.info("tenant deletion", extra={"deletions_run": ran})
        return ran

    async def deletion_record_purge(self) -> int:
        """Purge finished deletion records past `DELETION_RETENTION_DAYS` (singleton, leased).

        Retention 0 (the default) never purges, and with no `RecordPurger` composed the duty does
        nothing. Bounded by the purger (batches, and half the lease TTL), with a beat per batch.
        The database function deletes `cancelled` records and `done` records whose tombstone
        exists; an eligible `done` record without one is never deleted and is logged here at
        WARNING (ADR 0034, B4).
        """
        purger = self._d.record_purger
        if purger is None or self._s.deletion_retention_days == 0:
            return 0
        try:
            async with self._d.leases.hold(NIL_TENANT, CORE_ADAPTER, "deletion-purge"):
                self._beat("deletion-purge")
                result = await purger.purge(lambda _batch: self._beat("deletion-purge"))
        except LeaseBusy:
            return 0
        if result.unprotected:
            logger.warning(
                "finished tenant deletion records without a tombstone were left in place",
                extra={"unprotected_records": result.unprotected},
            )
        if result.purged:
            logger.info("deletion record purge", extra={"purged_records": result.purged})
        return result.purged

    async def tenant_reconcile(self) -> int:
        """Drain the tenant reconcile requests with claim/ack (replica-safe, no lease).

        First returns requests stranded inflight past `TENANT_RECONCILE_CLAIM_TTL_SECONDS` to the
        pending set, then claims at most `TENANT_RECONCILE_BATCH_SIZE`. Per claim: a tenant that
        is disabled, deleting or gone is acknowledged and dropped with a log line (ADR 0033
        invariant 19, ADR 0034); otherwise, with the body composed and
        `TENANT_RECONCILE_ENABLED` true, the body runs and the claim is acknowledged. Without the
        body the request goes back to pending: a request already answered 202 is never consumed
        by a duty that does nothing. A body or database failure leaves the claim inflight, so it is
        retried after the claim TTL (the backoff); each run counts, and the request is dropped,
        at ERROR, once `TENANT_RECONCILE_MAX_ATTEMPTS` runs have not succeeded. A body whose claim
        was reclaimed (`ClaimLostError` from its `extend`) stops before its next write step. A run
        starts no body after half the claim TTL (the rest of its claims go back to pending), so a
        claim never expires before its body starts.
        Returns the number of requests acknowledged.
        """
        queue, refusals = self._d.reconcile_requests, self._d.refusals
        if queue is None or refusals is None:
            return 0
        s = self._s
        now = self._d.clock()
        await queue.reclaim(now, s.tenant_reconcile_claim_ttl_seconds)
        body = self._d.tenant_reconciler if s.tenant_reconcile_enabled else None
        # Every claim of this run is stamped `now`, so claims not started within half the TTL go
        # back to pending, to be claimed fresh by the next run (or another replica).
        deadline = now + timedelta(seconds=s.tenant_reconcile_claim_ttl_seconds / 2)
        acked = kept = deferred = 0
        for claim in await queue.claim(now, s.tenant_reconcile_batch_size):
            self._beat("tenant-reconcile")
            try:
                if self._d.clock() >= deadline:
                    deferred += await queue.release(claim)
                    continue
                done = await self._reconcile_claim(queue, refusals, body, claim)
            except ClaimLostError:
                logger.warning(
                    "tenant reconcile aborted: its claim was reclaimed while it ran",
                    extra={"tenant": str(claim.tenant_id)},
                )
                continue
            except Exception:
                logger.exception(
                    "tenant reconcile failed; the claim is reclaimed after its TTL",
                    extra={"tenant": str(claim.tenant_id)},
                )
                continue
            acked += done
            kept += not done
        self._beat("tenant-reconcile")
        if deferred:
            logger.info(
                "reconcile requests returned to pending: run budget used",
                extra={"count": deferred},
            )
        if kept:
            logger.info("reconcile requests left pending: no reconcile body", extra={"count": kept})
        return acked

    async def _reconcile_claim(
        self,
        queue: ReconcileQueue,
        refusals: TenantRefusals,
        body: TenantReconciler | None,
        claim: Claim,
    ) -> bool:
        """Settle one claim; True when it was acknowledged, False when it was left pending."""
        tenant = str(claim.tenant_id)
        ran = False
        refusal = await refusals.refusal_of(claim.tenant_id)
        if refusal is not None:
            logger.info(
                "dropped a reconcile request: tenant is %s",
                refusal,
                extra={"tenant": tenant, "reason": refusal},
            )
        elif body is None:
            await queue.release(claim)
            return False
        else:
            ran = await self._run_body(queue, body, claim)  # a poison request returns, to be acked
        acked = await queue.ack(claim)
        if not acked:
            logger.warning(
                "reconcile claim was reclaimed while it ran; the request runs again",
                extra={"tenant": tenant},
            )
        if acked or ran:  # a body that succeeded but lost its ack is not a failure to count
            await queue.clear_attempts(claim)
        return True

    async def _run_body(self, queue: ReconcileQueue, body: TenantReconciler, claim: Claim) -> bool:
        """Run the body for a claim; False for a poison request, which is logged and left to ack.

        Each run counts first (a crash counts too). The request is dropped, at ERROR, when it has
        already run `TENANT_RECONCILE_MAX_ATTEMPTS` times without success; a failure leaves the
        claim inflight, so the next attempt waits the claim TTL (the backoff).
        """
        attempt = await queue.begin_attempt(claim)
        limit = self._s.tenant_reconcile_max_attempts
        if attempt > limit:
            logger.error(
                "dropped a reconcile request: it failed %d times",
                limit,
                extra={"tenant": str(claim.tenant_id), "attempts": limit},
            )
            return False

        async def extend() -> None:
            self._beat("tenant-reconcile")
            if not await queue.extend(claim, self._d.clock()):
                raise ClaimLostError(str(claim.tenant_id))

        await body.reconcile(claim.tenant_id, extend)
        return True

    async def trim_streams(self) -> None:
        """Trim `tb:events` below what the group acknowledged, the dead letter by age, set lag."""
        await self._d.queue.trim_events()
        self._beat("trim")
        cutoff = self._d.clock() - timedelta(days=self._s.dead_stream_retention_days)
        await self._d.queue.trim_dead(int(cutoff.timestamp() * 1000))
        self._beat("trim")
        metrics.set_stream_lag(await self._d.queue.lag())

    async def run(self, stop: asyncio.Event) -> None:
        """Run every job on its own interval until `stop` is set."""
        s = self._s
        beat = self._d.heartbeat or Heartbeat()
        schedule: list[tuple[str, float, Job]] = [
            ("relay-tick", s.relay_interval_seconds, self.relay_tick),
            ("resweep", s.relay_recheck_seconds, self.resweep),
            ("deliveries", s.delivery_reconcile_interval_seconds, self.reconcile_deliveries),
            ("retention", s.retention_interval_seconds, self.retention),
            ("audit-purge", s.audit_purge_interval_seconds, self.audit_purge),
            ("tenant-deletion", s.deletion_poll_seconds, self.tenant_deletion),
            ("deletion-purge", s.deletion_purge_interval_seconds, self.deletion_record_purge),
            ("tenant-reconcile", s.tenant_reconcile_interval_seconds, self.tenant_reconcile),
            ("trim", s.stream_trim_interval_seconds, self.trim_streams),
        ]
        for name, interval, _job in schedule:
            limit = stale_after(s, interval)
            if name == "tenant-deletion":  # a pass runs up to its own lease budget between beats
                limit = interval + max(s.lease_ttl_seconds, s.deletion_lease_ttl_seconds)
            beat.register(name, limit)
        if self._d.audit_sessions is None:
            logger.info("audit purge disabled: no audit store")
        if self._d.deletion is None:
            logger.info("tenant deletion disabled: no deletion engine")
        if self._d.reconcile_requests is None or self._d.refusals is None:
            logger.info("tenant reconcile disabled: no Valkey or database")
        elif not (s.tenant_reconcile_enabled and self._d.tenant_reconciler is not None):
            logger.info("tenant reconcile requests stay pending: no reconcile body is enabled")
        if self._d.record_purger is None:
            logger.info("deletion record purge disabled: no record purger")
        await self._d.queue.ensure_group()  # registered first: a hang here is a stale loop
        jobs = [periodic(name, interval, job, stop, beat.beat) for name, interval, job in schedule]
        await asyncio.gather(*jobs)
