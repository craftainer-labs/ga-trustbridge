"""A worker/relay/reconciler harness over the in-memory store and the fake Valkey."""

import json
from typing import Any

from tests.trustbridge.fakes import Clock, FakeHostChecker, FakeRedis
from tests.trustbridge.helpers import as_redis, host, make_settings
from trustbridge.settings import GitHubHostConfig, TrustbridgeSettings
from trustbridge.worker.deliveries import InMemoryDeliveryStore, NewDelivery
from trustbridge.worker.hosts import HostAvailability
from trustbridge.worker.lease import InMemoryLeaseStore, LeaseManager
from trustbridge.worker.queue import StreamQueue
from trustbridge.worker.relay import Relay
from trustbridge.worker.tenants import InMemoryTenantResolver
from trustbridge.worker.worker import HandlerMap, Worker, WorkerDeps


class WorkerDied(BaseException):  # noqa: N818 -- simulates the process ending mid-handling
    """Raised by a handler to model the worker process dying before it can record anything."""


class Harness:
    """Everything a worker test needs, with one clock for Postgres-side and Valkey-side time."""

    def __init__(
        self,
        handlers: HandlerMap | None = None,
        hosts: list[GitHubHostConfig] | None = None,
        failing_hosts: set[str] | None = None,
        **settings_kw: Any,  # noqa: ANN401
    ) -> None:
        self.settings: TrustbridgeSettings = make_settings(
            hosts or [host("ok"), host("bad")],
            **{
                "max_delivery_attempts": 3,
                "retry_backoff_base_seconds": 5,
                "retry_backoff_max_seconds": 900,
                "stream_claim_idle_seconds": 60,
                **settings_kw,
            },
        )
        self.clock = Clock()
        self.valkey = FakeRedis()
        self.queue = StreamQueue(as_redis(self.valkey))
        self.store = InMemoryDeliveryStore()
        self.relay = Relay(self.store, self.queue, clock=self.clock)
        self.checker = FakeHostChecker(failing=failing_hosts or set())
        self.hosts = HostAvailability(
            lambda: self.settings.github_hosts, self.checker, check_timeout=1
        )
        self.leases = LeaseManager(InMemoryLeaseStore(), ttl_seconds=120)
        self.calls: list[str] = []
        self.worker = Worker(
            self.settings,
            WorkerDeps(
                store=self.store,
                queue=self.queue,
                relay=self.relay,
                hosts=self.hosts,
                tenants=InMemoryTenantResolver(),
                handlers=handlers if handlers is not None else {},
                clock=self.clock,
            ),
            "c1",
        )

    async def start(self) -> "Harness":
        """Check the hosts and create the consumer group."""
        await self.hosts.check_all()
        await self.queue.ensure_group()
        return self

    def advance(self, seconds: float) -> None:
        """Move the Postgres-side and Valkey-side clocks together."""
        self.clock.advance(seconds=seconds)
        self.valkey.advance(int(seconds * 1000))

    async def add(
        self, delivery_id: str, *, host_name: str = "ok", event: str = "push", relay: bool = True
    ) -> None:
        """Store a delivery (as ingress would) and relay it."""
        body = json.dumps({"installation": {"id": 7}}).encode()
        await self.store.insert(
            NewDelivery(delivery_id, host_name, event, {}, body, self.clock())
        )
        if relay:
            await self.relay.tick()
