"""The stream consumer (ADR 0028 "Worker"; invariants 3 and 4; ADR 0030 invariant 8).

Handler interface: register `async def handler(ctx: EventContext) -> None` per GitHub event name
in the `HandlerRegistry` (`trustbridge.github.handlers`; the core's lifecycle handlers are
registered by `register_core_handlers`, adapters add theirs). A handler must be
idempotent. Raise `LeaseBusy` (from `trustbridge.worker.lease`) when a scope lease is taken:
the delivery is deferred and no failure is counted. Any other exception is a handler
failure: the failure counter increases,
the delivery is retried with exponential backoff, and after `MAX_DELIVERY_ATTEMPTS` failures
it is dead-lettered. An event with no registered handler is processed as a no-op.

What does not count as a failure (invariant 3): claiming an entry, a relay duplicate, a
re-relay, a busy lease, and a delivery whose host is not `ready` (the entry is acknowledged
without being claimed for handling; the row stays pending and the host-ready transition
re-adds it). What dead-letters without a recorded failure (invariant 4): an entry whose
stream delivery count exceeds `MAX_DELIVERY_ATTEMPTS`, which only happens when handling kept
ending the process (the entry stays pending and is claimed again after the idle time).
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import timedelta

from craftainer_observability.health.redact import log_failure

from trustbridge.github.availability import HostMonitor
from trustbridge.github.handlers import EventContext, HandlerRegistry
from trustbridge.settings import TrustbridgeSettings
from trustbridge.worker import metrics
from trustbridge.worker.deliveries import PENDING, DeliveryRecord, DeliveryStore, retry_at
from trustbridge.worker.lease import LeaseBusy
from trustbridge.worker.queue import StreamEntry, StreamQueue
from trustbridge.worker.relay import Clock, Relay, utcnow

logger = logging.getLogger(__name__)
MAX_ERROR_CHARS = 500


@dataclass(slots=True)
class WorkerDeps:
    """Collaborators of a `Worker`."""

    store: DeliveryStore
    queue: StreamQueue
    relay: Relay
    hosts: HostMonitor
    registry: HandlerRegistry
    clock: Clock = utcnow


class Worker:
    """One consumer in the `tb-workers` group."""

    def __init__(self, settings: TrustbridgeSettings, deps: WorkerDeps, consumer: str) -> None:
        """Bind settings, collaborators and this consumer's name."""
        self._s = settings
        self._d = deps
        self.consumer = consumer

    async def run(self, stop: asyncio.Event) -> None:
        """Consume until `stop` is set."""
        await self._d.queue.ensure_group()
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("stream read failed; backing off")
                await asyncio.sleep(1.0)

    async def run_once(self) -> int:
        """One iteration: claim stale pending entries, read new ones, process them all."""
        idle_ms = int(self._s.stream_claim_idle_seconds * 1000)
        claimed = await self._d.queue.claim_stale(
            self.consumer, min_idle_ms=idle_ms, count=self._s.worker_batch_size
        )
        fresh = await self._d.queue.read(
            self.consumer, count=self._s.worker_batch_size, block_ms=self._s.worker_block_ms
        )
        await asyncio.gather(
            *(self.process(e, from_pending=True) for e in claimed),
            *(self.process(e, from_pending=False) for e in fresh),
        )
        return len(claimed) + len(fresh)

    async def process(self, entry: StreamEntry, *, from_pending: bool) -> None:
        """Process one entry; infrastructure errors re-add the id and never count a failure."""
        try:
            await self._process(entry, from_pending=from_pending)
        except Exception:
            logger.exception(
                "processing infrastructure error", extra={"delivery_id": entry.delivery_id}
            )
            try:
                await self._d.queue.add(entry.delivery_id)
                await self._d.queue.ack(entry.entry_id)
            except Exception:
                logger.exception("could not re-add the entry; it stays pending")

    async def _process(self, entry: StreamEntry, *, from_pending: bool) -> None:
        """The decision tree for one stream entry."""
        queue, store = self._d.queue, self._d.store
        row = await store.get(entry.delivery_id)
        if row is None or row.status != PENDING:
            await queue.ack(entry.entry_id)
            return
        now = self._d.clock()
        if not self._d.hosts.is_ready(row.host) or (
            row.next_attempt_at is not None and row.next_attempt_at > now
        ):
            await queue.ack(entry.entry_id)  # not claimed for handling; nothing is counted
            return
        if (
            from_pending
            and await queue.delivery_count(entry.entry_id) > self._s.max_delivery_attempts
        ):
            await self._dead(row, entry, "worker ended while handling this delivery repeatedly")
            return
        try:
            await self._handle(row)
        except LeaseBusy:
            await store.defer(row.delivery_id, now + timedelta(seconds=self._s.busy_retry_seconds))
            await queue.ack(entry.entry_id)
        except Exception as exc:
            log_failure(logger, "delivery handler failed", exc)
            error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
            await self._failed(row, entry, error)
        else:
            await store.mark_processed(row.delivery_id, self._d.clock())
            await queue.ack(entry.entry_id)

    async def _failed(self, row: DeliveryRecord, entry: StreamEntry, error: str) -> None:
        """Count a handler failure; retry later or dead-letter at the limit."""
        attempts = row.attempts + 1
        metrics.deliveries_failed.add(1)
        when = retry_at(
            self._d.clock(),
            attempts,
            self._s.retry_backoff_base_seconds,
            self._s.retry_backoff_max_seconds,
        )
        await self._d.store.record_failure(row.delivery_id, error, self._d.clock(), when)
        if attempts >= self._s.max_delivery_attempts:
            await self._dead(row, entry, error)
        else:
            await self._d.queue.ack(entry.entry_id)

    async def _dead(self, row: DeliveryRecord, entry: StreamEntry, error: str) -> None:
        """Mark dead in Postgres, add `{delivery_id, error}` to the dead letter, acknowledge."""
        await self._d.store.mark_dead(row.delivery_id, error, self._d.clock())
        await self._d.queue.dead_letter(row.delivery_id, error)
        await self._d.queue.ack(entry.entry_id)
        metrics.deliveries_dead.add(1)
        logger.error("delivery dead-lettered", extra={"delivery_id": row.delivery_id})

    async def _handle(self, row: DeliveryRecord) -> None:
        """Parse the stored body and dispatch it to the handlers registered for the event."""
        if not self._d.registry.handlers_for(row.event):
            return
        if row.body is None:
            raise ValueError("delivery body was purged")
        payload = json.loads(row.body)
        if not isinstance(payload, dict):
            raise TypeError("webhook payload is not a JSON object")
        action = payload.get("action")
        await self._d.registry.dispatch(
            EventContext(
                host=row.host,
                delivery_id=row.delivery_id,
                event=row.event,
                action=action if isinstance(action, str) else None,
                payload=payload,
            )
        )
