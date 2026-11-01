"""The reconciler process: relay ticks, re-relay sweep, delivery reconciliation, retention.

GitHub never redelivers on its own (ADR 0028 item 4). The delivery reconciler lists failed
hook deliveries per `ready` host with the App JWT endpoints (`GET /app/hook/deliveries`,
`POST /app/hook/deliveries/{id}/attempts`) through the `HookDeliveryClient` Protocol (NG4
implements it) and redelivers only those whose `X-GitHub-Delivery` guid has **no stored
row** (invariant 8); a stored row is durable and owned by the relay and the worker.

Singleton duties (delivery reconciliation per host, DB retention) run under a lease row so
replicas do not duplicate them (EB-6); the relay tick, the re-relay sweep and stream trimming
are idempotent and run on every replica.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from trustbridge.settings import GitHubHostConfig, TrustbridgeSettings
from trustbridge.worker import metrics
from trustbridge.worker.deliveries import DeliveryStore
from trustbridge.worker.hosts import HostAvailability
from trustbridge.worker.lease import LeaseBusy, LeaseManager
from trustbridge.worker.models import NIL_TENANT
from trustbridge.worker.queue import StreamQueue
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
        failed = [
            d for d in latest.values() if not HTTP_OK_LOW <= d.status_code < HTTP_OK_HIGH
        ]
        if not failed:
            return 0
        stored = await self._store.existing_ids([d.guid for d in failed])
        redelivered = 0
        for item in failed:
            if item.guid in stored or redelivered >= self._max:
                continue
            if await self._store.existing_ids([item.guid]):  # re-check just before the call
                continue
            await self._client.redeliver(host, item.id)
            redelivered += 1
        if redelivered:
            logger.info("redelivered failed deliveries", extra={"host": host.name, "count": redelivered})
        return redelivered


Job = Callable[[], Awaitable[object]]


async def periodic(name: str, interval: float, job: Job, stop: asyncio.Event) -> None:
    """Run `job` now and then every `interval` seconds until `stop`; errors are logged."""
    while not stop.is_set():
        try:
            await job()
        except Exception:
            logger.exception("reconciler job failed", extra={"job": name})
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)


@dataclass(slots=True)
class ReconcilerDeps:
    """Collaborators of a `Reconciler`."""

    store: DeliveryStore
    queue: StreamQueue
    relay: Relay
    hosts: HostAvailability
    leases: LeaseManager
    deliveries: HookDeliveryClient | None
    clock: Clock = utcnow


class Reconciler:
    """Runs the periodic jobs of the reconciler process."""

    def __init__(self, settings: TrustbridgeSettings, deps: ReconcilerDeps) -> None:
        """Bind settings and collaborators."""
        self._s = settings
        self._d = deps

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
            host = self._d.hosts.config(name)
            if host is None or not self._d.hosts.is_ready(name):
                continue
            try:
                async with self._d.leases.hold(NIL_TENANT, CORE_ADAPTER, f"reconcile-deliveries:{name}"):
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
                purged = await self._d.store.purge_bodies(
                    now - timedelta(days=self._s.body_retention_processed_days),
                    now - timedelta(days=self._s.body_retention_dead_days),
                )
                deleted = await self._d.store.delete_expired(
                    now - timedelta(days=self._s.delivery_id_retention_days)
                )
                logger.info("retention", extra={"purged_bodies": purged, "deleted_rows": deleted})
        except LeaseBusy:
            return

    async def trim_streams(self) -> None:
        """Trim `tb:events` below what the group acknowledged, the dead letter by age, set lag."""
        await self._d.queue.trim_events()
        cutoff = self._d.clock() - timedelta(days=self._s.dead_stream_retention_days)
        await self._d.queue.trim_dead(int(cutoff.timestamp() * 1000))
        metrics.set_stream_lag(await self._d.queue.lag())

    async def run(self, stop: asyncio.Event) -> None:
        """Run every job on its own interval until `stop` is set."""
        await self._d.queue.ensure_group()
        s = self._s
        jobs = [
            periodic("relay-tick", s.relay_interval_seconds, self.relay_tick, stop),
            periodic("resweep", s.relay_recheck_seconds, self.resweep, stop),
            periodic(
                "deliveries", s.delivery_reconcile_interval_seconds, self.reconcile_deliveries, stop
            ),
            periodic("retention", s.retention_interval_seconds, self.retention, stop),
            periodic("trim", s.stream_trim_interval_seconds, self.trim_streams, stop),
        ]
        await asyncio.gather(*jobs)
