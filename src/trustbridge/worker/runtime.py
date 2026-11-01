"""Shared process bootstrap for the api lifespan, the worker and the reconciler.

`build_core` assembles the store, queue, relay, leases and the GitHub side of the process:
the ports over NG3's components (`trustbridge.worker.ports`) go into
`trustbridge.github.runtime.build_host_runtime`, which returns the `HostMonitor`, gateway,
hook-delivery client and `HandlerRegistry`. `run_process` is the `main()` of the worker and
reconciler commands: it waits for the schema, runs the host startup checks (`monitor.check_all`),
starts the recheck task (`monitor.start`), runs, and stops the monitor. The worker's monitor
re-adds a host's waiting rows when the host turns `ready` (`MonitorDeps.on_ready`). No process
here ever runs a migration (invariant 17); only `trustbridge.worker.migrate` does.
"""

import asyncio
import logging
import signal
import socket
import uuid
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Literal

from craftainer_observability.logs import configure_logging
from redis.asyncio import Redis
from redis.asyncio import from_url as redis_from_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.adapters.interface import GitHubHost
from trustbridge.github.availability import HostChecker
from trustbridge.github.hosts import GitHubHostConfig
from trustbridge.github.models import SqlRegistrationStore
from trustbridge.github.runtime import HostRuntime, RuntimeDeps, build_host_runtime
from trustbridge.settings import TrustbridgeSettings, load_trustbridge_settings
from trustbridge.worker.deliveries import DeliveryStore, InMemoryDeliveryStore, PgDeliveryStore
from trustbridge.worker.lease import InMemoryLeaseStore, LeaseManager, LeaseStore, PgLeaseStore
from trustbridge.worker.ports import (
    ConnectionData,
    MemoryTenants,
    ParkedValues,
    ReconcileRequests,
    SqlTenants,
)
from trustbridge.worker.queue import StreamQueue
from trustbridge.worker.reconciler import Reconciler, ReconcilerDeps
from trustbridge.worker.relay import Relay
from trustbridge.worker.schema import SchemaGate, SchemaWaitTimeout
from trustbridge.worker.secrets import LocalSecrets, MemorySecrets
from trustbridge.worker.worker import Worker, WorkerDeps

logger = logging.getLogger(__name__)
ProcessKind = Literal["worker", "reconciler"]


@dataclass(slots=True)
class Core:
    """The assembled collaborators."""

    settings: TrustbridgeSettings
    store: DeliveryStore
    leases: LeaseManager
    tenants: SqlTenants | MemoryTenants
    secrets: LocalSecrets | MemorySecrets
    parked: ParkedValues
    connections: ConnectionData
    gate: SchemaGate
    runtime: HostRuntime
    queue: StreamQueue | None
    relay: Relay | None


def session_factory_or_none(settings: TrustbridgeSettings) -> async_sessionmaker[AsyncSession] | None:
    """The template's session factory, or None in mock mode (no database is touched)."""
    if settings.mode == "mock":
        return None
    from app.db import session_factory  # noqa: PLC0415 -- importing builds the engine

    return session_factory


def open_redis(settings: TrustbridgeSettings) -> Redis:
    """A redis-py asyncio client against the Valkey server (`REDIS_URL`, `rediss://` in prod)."""
    return redis_from_url(settings.redis_url, decode_responses=True)


def build_core(
    settings: TrustbridgeSettings,
    *,
    redis: Redis | None,
    session_factory: async_sessionmaker[AsyncSession] | None,
    runtime: HostRuntime | None = None,
    readd_on_ready: bool = False,
    checker: HostChecker | None = None,
) -> Core:
    """Assemble the store, queue, relay, leases, GitHub runtime and schema gate.

    `readd_on_ready` (the worker) makes a host that turns `ready` re-add its waiting rows to
    the stream (ADR 0030 invariant 8). A given `runtime` replaces the real GitHub side (tests).
    """
    store: DeliveryStore
    lease_store: LeaseStore
    tenants: SqlTenants | MemoryTenants
    registrations: SqlRegistrationStore | None = None
    if session_factory is None:
        store, lease_store = InMemoryDeliveryStore(), InMemoryLeaseStore()
    else:
        store, lease_store = PgDeliveryStore(session_factory), PgLeaseStore(session_factory)
        registrations = SqlRegistrationStore(session_factory)
    host_by_name = {h.name: h for h in settings.github_hosts}

    def host_projection(name: str) -> GitHubHost:
        return host_by_name[name].to_host()

    tenants = (
        MemoryTenants(host_projection)
        if session_factory is None
        else SqlTenants(session_factory, host_projection)
    )
    secrets: LocalSecrets | MemorySecrets = (
        MemorySecrets() if settings.mode == "mock" else LocalSecrets(settings.secret_store_dir)
    )
    queue = StreamQueue(redis) if redis is not None else None
    relay = (
        Relay(store, queue, batch_size=settings.relay_batch_size) if queue is not None else None
    )

    async def readd(host: GitHubHostConfig) -> None:
        if relay is not None:
            count = await relay.relay_host(host.name)
            logger.info("re-relayed rows of a host that became ready", extra={"host": host.name, "count": count})

    runtime = runtime or build_host_runtime(
        settings,
        RuntimeDeps(
            installations=tenants,
            installation_ids=tenants,
            scheduler=ReconcileRequests(redis),
            registrations=registrations,
            on_ready=readd if readd_on_ready else None,
            checker=checker,
        ),
    )
    return Core(
        settings=settings,
        store=store,
        leases=LeaseManager(lease_store, ttl_seconds=settings.lease_ttl_seconds),
        tenants=tenants,
        secrets=secrets,
        parked=ParkedValues(secrets),
        connections=ConnectionData(secrets),
        gate=SchemaGate(session_factory, mock=settings.mode == "mock"),
        runtime=runtime,
        queue=queue,
        relay=relay,
    )


def consumer_name() -> str:
    """A unique stream consumer name for this process."""
    return f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


@dataclass(slots=True)
class ProcessOverrides:
    """Collaborators a test can substitute for `run_process`."""

    redis: Redis | None = None
    session_factory: async_sessionmaker[AsyncSession] | None = None
    runtime: HostRuntime | None = None
    checker: HostChecker | None = None
    stop: asyncio.Event | None = None


async def run_process(
    kind: ProcessKind,
    settings: TrustbridgeSettings | None = None,
    overrides: ProcessOverrides | None = None,
) -> int:
    """Run a worker or reconciler until SIGTERM/SIGINT; returns the process exit code.

    Invalid settings raise (non-zero exit). The schema gate waits up to `SCHEMA_WAIT_SECONDS`
    (the process stays alive meanwhile) and returns 1 if the schema never becomes current.
    The host startup checks and the recheck loop are the same as the api's.
    """
    settings = settings or load_trustbridge_settings()
    chosen = overrides or ProcessOverrides()
    stop = chosen.stop or asyncio.Event()
    redis = chosen.redis
    session_factory = chosen.session_factory
    _install_signal_handlers(stop)
    async with AsyncExitStack() as stack:
        client = redis
        if client is None:
            client = open_redis(settings)
            stack.push_async_callback(client.aclose)
        sessions = session_factory if session_factory is not None else session_factory_or_none(settings)
        core = build_core(
            settings,
            redis=client,
            session_factory=sessions,
            runtime=chosen.runtime,
            readd_on_ready=kind == "worker",
            checker=chosen.checker,
        )
        if core.queue is None or core.relay is None:  # pragma: no cover -- a client was given
            raise RuntimeError("the stream queue is not configured")
        try:
            await core.gate.wait(timeout=settings.schema_wait_seconds, poll=settings.schema_poll_seconds)
        except SchemaWaitTimeout:
            logger.critical("schema not current after SCHEMA_WAIT_SECONDS; exiting")
            return 1
        monitor = core.runtime.monitor
        await monitor.check_all()
        monitor.start(settings.host_recheck_seconds)
        try:
            if kind == "worker":
                deps = WorkerDeps(
                    store=core.store,
                    queue=core.queue,
                    relay=core.relay,
                    hosts=monitor,
                    registry=core.runtime.registry,
                )
                await Worker(settings, deps, consumer_name()).run(stop)
            else:
                rdeps = ReconcilerDeps(
                    store=core.store,
                    queue=core.queue,
                    relay=core.relay,
                    hosts=monitor,
                    leases=core.leases,
                    deliveries=core.runtime.deliveries,
                )
                await Reconciler(settings, rdeps).run(stop)
        finally:
            await monitor.stop()
    return 0


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """Set `stop` on SIGTERM and SIGINT (a no-op where the loop cannot install handlers)."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover -- non-main thread
            logger.debug("cannot install a handler for %s", sig)


def main(kind: ProcessKind, runner: Callable[..., object] = asyncio.run) -> int:
    """Synchronous entry for `python -m trustbridge.worker [worker|reconciler]`."""
    configure_logging()
    return int(runner(run_process(kind)))  # type: ignore[call-overload]
