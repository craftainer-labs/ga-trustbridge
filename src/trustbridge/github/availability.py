"""Per-host availability (ADR 0030 item 5, option a): `unregistered`, `unavailable`, `ready`.

Only invalid settings stop a process (see `hosts.validate_hosts`). Everything here needs the
signer, the network or GitHub, so a failure marks one host `unavailable` with a reason and never
raises: the process stays alive and ready, other hosts are unaffected, and the failed checks
repeat every `HOST_RECHECK_SECONDS` in every process that runs a `HostMonitor` (api, worker,
reconciler), each holding its own view. The signer is not part of the api's readiness.
"""

import asyncio
import contextlib
import logging
import ssl
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import httpx

from trustbridge.github.app_jwt import (
    SELFTEST_PROBE,
    build_app_jwt,
    verify_rs256,
)
from trustbridge.github.client import GitHubApi, GitHubApiError, GitHubRequester
from trustbridge.github.hosts import DEFAULT_HOST_RECHECK_SECONDS, GitHubHostConfig
from trustbridge.github.models import Registration
from trustbridge.signer.base import AppKeySigner

_log = logging.getLogger(__name__)
_DISCOVERY_TIMEOUT = 10.0


class HostState(StrEnum):
    """Where a configured host is."""

    UNREGISTERED = "unregistered"
    UNAVAILABLE = "unavailable"
    READY = "ready"


@dataclass(frozen=True, slots=True)
class HostStatus:
    """A host's state and, when it is not ready, why. The reason never carries secrets."""

    state: HostState
    reason: str | None = None


class HostNotReadyError(Exception):
    """A GitHub or adapter call was attempted for a host that is not `ready`."""

    def __init__(self, host: str, status: HostStatus) -> None:
        """Record the host and its status."""
        super().__init__(f"host {host!r} is {status.state}")
        self.host = host
        self.status = status


class RegistrationStore(Protocol):
    """Readable by every process; see `models.SqlRegistrationStore`."""

    async def load_all(self) -> Mapping[str, Registration]:
        """Return every registration by host name."""
        ...


class HostDirectory(Protocol):
    """Looks up the effective configuration and status of hosts."""

    def config(self, name: str) -> GitHubHostConfig:
        """Return the effective config (registration overlaid); KeyError if unknown."""
        ...

    def status(self, name: str) -> HostStatus:
        """Return the host's status."""
        ...


class HostChecker(Protocol):
    """The network checks of ADR 0030 (checks 2 and 3), replaceable in tests."""

    async def get_app_id(self, host: GitHubHostConfig, jwt: str) -> int:
        """Return the `id` that `GET /app` reports for this App JWT."""
        ...

    async def discovery_issuer(self, host: GitHubHostConfig) -> str:
        """Return the `issuer` of the host's OIDC discovery document."""
        ...


class HttpHostChecker:
    """`HostChecker` over githubkit (`GET /app`) and httpx (OIDC discovery, through the host CA)."""

    def __init__(
        self,
        api_factory: Callable[[GitHubHostConfig], GitHubRequester] | None = None,
        discovery_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """`api_factory` and `discovery_transport` are replaceable for tests."""
        self._api_factory = api_factory or GitHubApi
        self._discovery_transport = discovery_transport

    async def get_app_id(self, host: GitHubHostConfig, jwt: str) -> int:
        """Call `GET /app` as the App."""
        resp = await self._api_factory(host).request_as_app("GET", "/app", jwt=jwt)
        return int(resp.data["id"])

    async def discovery_issuer(self, host: GitHubHostConfig) -> str:
        """Fetch `<issuer>/.well-known/openid-configuration` and return its `issuer`."""
        verify: ssl.SSLContext | bool = True
        if host.ca_bundle is not None:
            verify = ssl.create_default_context(cafile=str(host.ca_bundle))
        url = host.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
        async with httpx.AsyncClient(
            timeout=_DISCOVERY_TIMEOUT, verify=verify, transport=self._discovery_transport
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return str(resp.json()["issuer"])


def effective_config(
    base: GitHubHostConfig, registrations: Mapping[str, Registration]
) -> GitHubHostConfig:
    """Overlay a stored registration on an entry that has no App; an entry with an App wins."""
    reg = registrations.get(base.name)
    if base.registered or reg is None:
        return base
    return base.with_registration(reg.app_id, reg.webhook_secret_ref)


@dataclass(frozen=True, slots=True)
class MonitorDeps:
    """Optional collaborators of `HostMonitor`.

    `on_ready` is called when a host turns `ready` in this process; the worker passes a hook that
    re-adds the host's waiting delivery rows to the stream (ADR 0030, invariant 8).
    """

    checker: HostChecker | None = None
    registrations: RegistrationStore | None = None
    on_ready: Callable[[GitHubHostConfig], Awaitable[None]] | None = None


class HostMonitor:
    """Holds each host's status in this process and re-runs the failed checks."""

    def __init__(
        self,
        hosts: Sequence[GitHubHostConfig],
        signers: Mapping[str, AppKeySigner],
        deps: MonitorDeps | None = None,
    ) -> None:
        """Start every host as unchecked: unregistered when it has no App, else unavailable."""
        deps = deps or MonitorDeps()
        self._base = {h.name: h for h in hosts}
        self._signers = signers
        self._checker = deps.checker or HttpHostChecker()
        self._registrations = deps.registrations
        self._on_ready = deps.on_ready
        self._config = dict(self._base)
        self._status: dict[str, HostStatus] = {
            h.name: (
                HostStatus(HostState.UNAVAILABLE, "not checked yet")
                if h.registered
                else HostStatus(HostState.UNREGISTERED, "no App registered")
            )
            for h in hosts
        }
        self._task: asyncio.Task[None] | None = None

    # ---- HostDirectory --------------------------------------------------
    def config(self, name: str) -> GitHubHostConfig:
        """Return the effective config of a host."""
        return self._config[name]

    def status(self, name: str) -> HostStatus:
        """Return a host's status."""
        return self._status[name]

    def statuses(self) -> dict[str, HostStatus]:
        """Return every host's status (the admin API and the metric)."""
        return dict(self._status)

    def require_ready(self, name: str) -> GitHubHostConfig:
        """Return the config, or raise `HostNotReadyError` (no call is made for such a host)."""
        status = self._status[name]
        if status.state is not HostState.READY:
            raise HostNotReadyError(name, status)
        return self._config[name]

    # ---- checks ---------------------------------------------------------
    async def check_all(self) -> None:
        """Check every host (startup)."""
        await self._check(list(self._base))

    async def recheck(self) -> None:
        """Re-check every host that is not ready (ready hosts get no probe, so no GitHub call)."""
        pending = [n for n, s in self._status.items() if s.state is not HostState.READY]
        await self._check(pending)

    async def _check(self, names: Sequence[str]) -> None:
        registrations: Mapping[str, Registration] = {}
        if self._registrations is not None:
            try:
                registrations = await self._registrations.load_all()
            except Exception:
                _log.warning("could not read host registrations; keeping the previous view")
                registrations = {
                    n: Registration(c.app_id, c.webhook_secret_ref)
                    for n, c in self._config.items()
                    if c.app_id is not None and c.webhook_secret_ref is not None
                }
        for name in names:
            cfg = effective_config(self._base[name], registrations)
            self._config[name] = cfg
            before = self._status[name]
            after = await self._check_host(cfg)
            self._status[name] = after
            if after.state is HostState.READY and before.state is not HostState.READY:
                await self._notify_ready(cfg)

    async def _notify_ready(self, cfg: GitHubHostConfig) -> None:
        """Invariant 8: on the transition to ready the worker re-adds the host's waiting rows."""
        if self._on_ready is None:
            return
        try:
            await self._on_ready(cfg)
        except Exception:
            _log.warning("on_ready hook failed for host %s", cfg.name)

    async def _check_host(self, cfg: GitHubHostConfig) -> HostStatus:
        if cfg.app_id is None:
            return HostStatus(HostState.UNREGISTERED, "no App registered")
        try:
            reason = await self._selftest(cfg, cfg.app_id)
            if reason is None:
                reason = await self._issuer_check(cfg)
        except Exception as exc:
            return HostStatus(HostState.UNAVAILABLE, f"check failed: {type(exc).__name__}")
        if reason is not None:
            return HostStatus(HostState.UNAVAILABLE, reason)
        return HostStatus(HostState.READY)

    async def _selftest(self, cfg: GitHubHostConfig, app_id: int) -> str | None:
        """Invariant 1: a probe signature verifies, and `GET /app` returns the configured id."""
        signer = self._signers.get(cfg.signer)
        if signer is None:
            return f"signer {cfg.signer!r} is not available"
        signature = await signer.sign_jwt(SELFTEST_PROBE)
        if not verify_rs256(await signer.public_key_pem(), SELFTEST_PROBE, signature):
            return "signer self-test failed: signature does not verify against its public key"
        jwt = await build_app_jwt(signer, app_id)
        try:
            reported = await self._checker.get_app_id(cfg, jwt)
        except GitHubApiError as exc:
            return f"GitHub rejected the App JWT (HTTP {exc.status})"
        if reported != app_id:
            return "signer key is not the key registered for this host's app_id"
        return None

    async def _issuer_check(self, cfg: GitHubHostConfig) -> str | None:
        issuer = await self._checker.discovery_issuer(cfg)
        if issuer != cfg.oidc_issuer:
            return "OIDC discovery issuer differs from the configured oidc_issuer"
        return None

    # ---- recheck loop ---------------------------------------------------
    def start(self, interval: float = DEFAULT_HOST_RECHECK_SECONDS) -> None:
        """Start the recheck task (api lifespan, worker and reconciler `main()`)."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(interval))

    async def stop(self) -> None:
        """Cancel the recheck task and wait for it."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _loop(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.recheck()
            except Exception:
                _log.exception("host recheck round failed")
