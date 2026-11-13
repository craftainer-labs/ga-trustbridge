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
from trustbridge.worker.deliveries import (
    PENDING,
    DeliveryRecord,
    DeliveryStore,
    TenantGate,
    retry_at,
)
from trustbridge.worker.health import Heartbeat, stale_after
from trustbridge.worker.lease import LeaseBusy
from trustbridge.worker.queue import StreamEntry, StreamQueue
from trustbridge.worker.relay import Clock, Relay, utcnow

logger = logging.getLogger(__name__)
MAX_ERROR_CHARS = 500
LOOP = "consume"


@dataclass(slots=True)
class WorkerDeps:
    """Collaborators of a `Worker`."""

    store: DeliveryStore
    queue: StreamQueue
    relay: Relay
    hosts: HostMonitor
    registry: HandlerRegistry
    clock: Clock = utcnow
    gate: TenantGate | None = None
    heartbeat: Heartbeat | None = None


# A delivery of a tenant being deleted is checked this many times less often than one of a
# disabled tenant: a deletion is not undone by `enable`, so only its end (the tenant row gone or the
# deletion cancelled) changes the answer, and a drain can take a long while.
DELETING_RETRY_FACTOR = 4


class TenantDisabled(Exception):  # noqa: N818 -- a control-flow signal, not a failure
    """The delivery's tenant is admin-disabled: it is deferred and no failure is counted."""


class TenantDeleting(Exception):  # noqa: N818 -- a control-flow signal, not a failure
    """The delivery's tenant has a deletion in progress: deferred, no failure counted.

    Unlike `TenantDisabled`, `enable` does not make it deliverable: the deletion is either
    cancelled (then the tenant is handled again) or ends by removing the delivery or the tenant.
    """

    def __init__(self, *, first: bool) -> None:
        """`first` is True the first time this delivery is deferred for the deletion."""
        super().__init__("tenant deletion in progress")
        self.first = first


def _lost_claim(row: DeliveryRecord, outcome: str) -> None:
    """Log that the conditional status update found the row changed since it was claimed."""
    logger.warning(
        "delivery changed since it was claimed; status update skipped",
        extra={"host": row.host, "delivery_id": row.delivery_id, "outcome": outcome},
    )


class Worker:
    """One consumer in the `tb-workers` group."""

    def __init__(self, settings: TrustbridgeSettings, deps: WorkerDeps, consumer: str) -> None:
        """Bind settings, collaborators and this consumer's name."""
        self._s = settings
        self._d = deps
        self.consumer = consumer
        self._beat = deps.heartbeat or Heartbeat()

    async def run(self, stop: asyncio.Event) -> None:
        """Consume until `stop` is set."""
        self._beat.register(LOOP, stale_after(self._s, self._s.worker_block_ms / 1000))
        await self._d.queue.ensure_group()  # registered first: a hang here is a stale loop
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("stream read failed; backing off")
                await asyncio.sleep(1.0)
            self._beat.beat(LOOP)

    async def run_once(self) -> int:
        """One iteration: claim stale pending entries, read new ones, process them all."""
        self._beat.beat(LOOP)
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
                "processing infrastructure error",
                extra={"host": entry.key.host, "delivery_id": entry.key.delivery_id},
            )
            try:
                await self._d.queue.add(entry.key)
                await self._d.queue.ack(entry.entry_id)
            except Exception:
                logger.exception("could not re-add the entry; it stays pending")
        finally:
            self._beat.beat(LOOP)  # one entry is a unit of progress, so a long batch stays live

    async def _process(self, entry: StreamEntry, *, from_pending: bool) -> None:
        """The decision tree for one stream entry."""
        queue, store = self._d.queue, self._d.store
        row = await store.get(entry.key)
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
            await self._dead(
                row, entry, "worker ended while handling this delivery repeatedly", row.attempts
            )
            return
        try:
            await self._handle(row)
        except LeaseBusy:
            await store.defer(row.key, now + timedelta(seconds=self._s.busy_retry_seconds))
            await queue.ack(entry.entry_id)
        except TenantDisabled:
            # Stored and acknowledged, not handled: checked again after the retry delay, so
            # `enable` makes it deliverable again (ADR 0033 invariant 19).
            retry = now + timedelta(seconds=self._s.disabled_tenant_retry_seconds)
            await store.defer(row.key, retry)
            await queue.ack(entry.entry_id)
        except TenantDeleting as deleting:
            level = logging.INFO if deleting.first else logging.DEBUG
            logger.log(
                level,
                "deferring %s delivery: tenant deletion in progress",
                row.event,
                extra={"host": row.host, "delivery_id": row.delivery_id},
            )
            retry = now + timedelta(
                seconds=self._s.disabled_tenant_retry_seconds * DELETING_RETRY_FACTOR
            )
            await store.defer(row.key, retry)
            await queue.ack(entry.entry_id)
        except Exception as exc:
            log_failure(logger, "delivery handler failed", exc)
            error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
            await self._failed(row, entry, error)
        else:
            done = await store.mark_processed(row.key, self._d.clock(), attempts=row.attempts)
            if not done:
                _lost_claim(row, "processed")
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
        counted = await self._d.store.record_failure(
            row.key, error, self._d.clock(), when, attempts=row.attempts
        )
        if not counted:
            # the row moved on (requeued, settled elsewhere): the failure is not ours to count
            _lost_claim(row, "failure")
            await self._d.queue.ack(entry.entry_id)
            return
        if attempts >= self._s.max_delivery_attempts:
            await self._dead(row, entry, error, attempts)
        else:
            await self._d.queue.ack(entry.entry_id)

    async def _dead(
        self, row: DeliveryRecord, entry: StreamEntry, error: str, attempts: int
    ) -> None:
        """Mark dead in Postgres, add `{host, delivery_id, error}` to the dead letter, ack.

        `attempts` is the counter the row has now (after a counted failure, one more than the
        claim). If the row moved on meanwhile (requeued, processed by another consumer) nothing
        is changed or dead-lettered; the entry is only acknowledged.
        """
        if not await self._d.store.mark_dead(row.key, error, self._d.clock(), attempts=attempts):
            _lost_claim(row, "dead")
            await self._d.queue.ack(entry.entry_id)
            return
        await self._d.queue.dead_letter(row.key, error)
        await self._d.queue.ack(entry.entry_id)
        metrics.deliveries_dead.add(1)
        logger.error(
            "delivery dead-lettered", extra={"host": row.host, "delivery_id": row.delivery_id}
        )

    async def _handle(self, row: DeliveryRecord) -> None:
        """Parse the stored body and dispatch it to the handlers registered for the event."""
        if not self._d.registry.handlers_for(row.event):
            return
        if row.body is None:
            raise ValueError("delivery body was purged")
        payload = json.loads(row.body)
        if not isinstance(payload, dict):
            raise TypeError("webhook payload is not a JSON object")
        if not await self._admit(row, payload):
            return
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

    async def _admit(self, row: DeliveryRecord, payload: dict[str, object]) -> bool:
        """Record the delivery's tenant, then decide whether the handlers may run.

        The tenant id is stored on the row first (ADR 0034: a deletion finds every delivery of
        its tenant, including the ones deferred below). Returns False for a tombstoned
        installation: the event is acknowledged and dropped (no handler runs, no tenant is
        created) and the caller marks it processed. Raises `TenantDisabled` for an admin-disabled
        tenant, and `TenantDeleting` for one being deleted: logged at info the first time the
        delivery is deferred for it (its tenant was not recorded yet) and at debug after that.
        """
        installation = payload.get("installation")
        installation_id = installation.get("id") if isinstance(installation, dict) else None
        if self._d.gate is None or type(installation_id) is not int:
            return True
        standing = await self._d.gate.standing(row.host, installation_id)
        first_sight = row.tenant_id is None
        if standing.tenant_id is not None:
            await self._d.store.set_tenant(row.key, standing.tenant_id)
        if standing.refusal == "tombstone":
            logger.info(
                "dropping %s delivery of a deleted tenant's installation; the App must be "
                "uninstalled (a reinstall gets a new installation id and works)",
                row.event,
                extra={"host": row.host, "delivery_id": row.delivery_id},
            )
            return False
        if standing.refusal == "deleting":
            raise TenantDeleting(first=first_sight)
        if standing.disabled:
            raise TenantDisabled
        return True
