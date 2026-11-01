"""Shared process bootstrap for the api lifespan, the worker and the reconciler.

`HostRuntime` is the narrow hook NG4 fills: `trustbridge.github.runtime.build_host_runtime(
settings) -> HostRuntime`, discovered by import name so NG3 never imports NG4's package. If
that module does not exist, a fail-closed checker marks every registered host `unavailable`.
`build_core` assembles the store, queue, relay, leases and host registry from the settings;
`run_process` is the `main()` of the worker and reconciler commands. No process here ever
runs a migration (invariant 17); only `trustbridge.worker.migrate` does.
"""

import asyncio
import importlib
import logging
import signal
import socket
import uuid
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Literal

from craftainer_observability.logs import configure_logging
from redis.asyncio import Redis
from redis.asyncio import from_url as redis_from_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trustbridge.settings import TrustbridgeSettings, load_trustbridge_settings
from trustbridge.worker.deliveries import DeliveryStore, InMemoryDeliveryStore, PgDeliveryStore
from trustbridge.worker.hosts import (
    HostAvailability,
    HostChecker,
    HostsProvider,
    UninstalledHostChecker,
)
from trustbridge.worker.lease import InMemoryLeaseStore, LeaseManager, LeaseStore, PgLeaseStore
from trustbridge.worker.queue import StreamQueue
from trustbridge.worker.reconciler import HookDeliveryClient, Reconciler, ReconcilerDeps
from trustbridge.worker.relay import Relay
from trustbridge.worker.schema import SchemaGate, SchemaWaitTimeout
from trustbridge.worker.tenants import InMemoryTenantResolver, PgTenantResolver, TenantResolver
from trustbridge.worker.worker import HandlerMap, Worker, WorkerDeps

logger = logging.getLogger(__name__)
HOOK_MODULE = "trustbridge.github.runtime"
ProcessKind = Literal["worker", "reconciler"]


@dataclass(slots=True)
class HostRuntime:
    """What the GitHub side (NG4) plugs into the core processes."""

    checker: HostChecker
    deliveries: HookDeliveryClient | None = None
    hosts_provider: HostsProvider | None = None
    handlers: HandlerMap = field(default_factory=dict)


def load_host_runtime(settings: TrustbridgeSettings) -> HostRuntime:
    """Import `trustbridge.github.runtime` and call `build_host_runtime(settings)` if present."""
    try:
        module = importlib.import_module(HOOK_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name not in {"trustbridge.github", HOOK_MODULE}:
            raise
        logger.warning("no %s; every registered host will be unavailable", HOOK_MODULE)
        return HostRuntime(checker=UninstalledHostChecker())
    runtime: HostRuntime = module.build_host_runtime(settings)
    return runtime


@dataclass(slots=True)
class Core:
    """The assembled collaborators."""

    settings: TrustbridgeSettings
    store: DeliveryStore
    leases: LeaseManager
    tenants: TenantResolver
    hosts: HostAvailability
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
) -> Core:
    """Assemble the store, queue, relay, leases, host registry and schema gate."""
    runtime = runtime or load_host_runtime(settings)
    store: DeliveryStore
    lease_store: LeaseStore
    tenants: TenantResolver
    if session_factory is None:
        store, lease_store, tenants = InMemoryDeliveryStore(), InMemoryLeaseStore(), InMemoryTenantResolver()
    else:
        store = PgDeliveryStore(session_factory)
        lease_store = PgLeaseStore(session_factory)
        tenants = PgTenantResolver(session_factory)
    provider: HostsProvider = runtime.hosts_provider or (
        lambda: list(settings.github_hosts)
    )
    hosts = HostAvailability(provider, runtime.checker, check_timeout=settings.host_check_timeout_seconds)
    queue = StreamQueue(redis) if redis is not None else None
    relay = (
        Relay(store, queue, batch_size=settings.relay_batch_size) if queue is not None else None
    )
    return Core(
        settings=settings,
        store=store,
        leases=LeaseManager(lease_store, ttl_seconds=settings.lease_ttl_seconds),
        tenants=tenants,
        hosts=hosts,
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
        core = build_core(settings, redis=client, session_factory=sessions, runtime=chosen.runtime)
        if core.queue is None or core.relay is None:  # pragma: no cover -- a client was given
            raise RuntimeError("the stream queue is not configured")
        try:
            await core.gate.wait(timeout=settings.schema_wait_seconds, poll=settings.schema_poll_seconds)
        except SchemaWaitTimeout:
            logger.critical("schema not current after SCHEMA_WAIT_SECONDS; exiting")
            return 1
        await core.hosts.check_all()
        recheck = asyncio.create_task(core.hosts.run_recheck(settings.host_recheck_seconds))
        try:
            if kind == "worker":
                deps = WorkerDeps(
                    store=core.store,
                    queue=core.queue,
                    relay=core.relay,
                    hosts=core.hosts,
                    tenants=core.tenants,
                    handlers=core.runtime.handlers,
                )
                await Worker(settings, deps, consumer_name()).run(stop)
            else:
                rdeps = ReconcilerDeps(
                    store=core.store,
                    queue=core.queue,
                    relay=core.relay,
                    hosts=core.hosts,
                    leases=core.leases,
                    deliveries=core.runtime.deliveries,
                )
                await Reconciler(settings, rdeps).run(stop)
        finally:
            recheck.cancel()
            await asyncio.gather(recheck, return_exceptions=True)
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
