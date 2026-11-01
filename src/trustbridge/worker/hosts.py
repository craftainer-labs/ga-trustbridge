"""Per-host availability (ADR 0030 "Startup checks and readiness"): the registry and recheck.

Each configured host is `unregistered`, `unavailable` or `ready`. The settings-only checks are
fatal and live in `trustbridge.settings`; everything that needs the signer, the network or
GitHub is a `HostChecker` (implemented by NG4, see `HostRuntime`) and never raises out of
here: a failure sets the host `unavailable` with a reason. State is per process.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from trustbridge.settings import GitHubHostConfig

logger = logging.getLogger(__name__)


class HostState(StrEnum):
    """The three availability states of a host."""

    UNREGISTERED = "unregistered"
    UNAVAILABLE = "unavailable"
    READY = "ready"


@dataclass(frozen=True, slots=True)
class HostStatus:
    """A host's state and, when not ready, the reason."""

    state: HostState
    reason: str | None = None


class HostCheckFailed(Exception):  # noqa: N818 -- a result, named for what it says
    """A host check failed; the message is a short, secret-free reason."""


class HostChecker(Protocol):
    """Signer self-test, `GET /app` id match and OIDC issuer check for one host (NG4)."""

    async def check(self, host: GitHubHostConfig) -> None:
        """Return normally when all checks pass; raise `HostCheckFailed(reason)` otherwise."""
        ...


class UninstalledHostChecker:
    """Fail-closed stand-in used until a real checker (NG4) is installed."""

    async def check(self, host: GitHubHostConfig) -> None:
        """Always fail: no signer or GitHub check is available."""
        raise HostCheckFailed("no host checker installed")


ReadyListener = Callable[[str], Awaitable[None]]
HostsProvider = Callable[[], Sequence[GitHubHostConfig]]


class HostAvailability:
    """Holds each host's status and rechecks `unavailable` and `unregistered` hosts."""

    def __init__(
        self,
        hosts: HostsProvider,
        checker: HostChecker,
        *,
        check_timeout: float,
    ) -> None:
        """Bind the (re-readable) host list, the checker and the per-check timeout."""
        self._hosts = hosts
        self._checker = checker
        self._timeout = check_timeout
        self._status: dict[str, HostStatus] = {}
        self._configs: dict[str, GitHubHostConfig] = {}
        self._listeners: list[ReadyListener] = []
        self.last_check_at: datetime | None = None

    def add_ready_listener(self, listener: ReadyListener) -> None:
        """Call `listener(host_name)` whenever a host moves to `ready` from another state."""
        self._listeners.append(listener)

    def names(self) -> list[str]:
        """Names of every host seen so far."""
        return list(self._status)

    def config(self, name: str) -> GitHubHostConfig | None:
        """The current configuration of host `name`, or None when it is not configured."""
        return self._configs.get(name)

    def status(self, name: str) -> HostStatus | None:
        """The status of host `name`, or None when it is not configured."""
        return self._status.get(name)

    def is_ready(self, name: str) -> bool:
        """Whether host `name` is `ready` in this process."""
        status = self._status.get(name)
        return status is not None and status.state is HostState.READY

    async def _check_one(self, host: GitHubHostConfig) -> HostStatus:
        """Run the checker for one host; never raises."""
        if not host.registered:
            return HostStatus(HostState.UNREGISTERED, "no App registered")
        try:
            async with asyncio.timeout(self._timeout):
                await self._checker.check(host)
        except HostCheckFailed as exc:
            return HostStatus(HostState.UNAVAILABLE, str(exc) or "check failed")
        except TimeoutError:
            return HostStatus(HostState.UNAVAILABLE, "check timed out")
        except Exception as exc:
            logger.warning("host check raised", extra={"host": host.name}, exc_info=exc)
            return HostStatus(HostState.UNAVAILABLE, f"check raised {type(exc).__name__}")
        return HostStatus(HostState.READY)

    async def check_all(self, *, only_not_ready: bool = False) -> None:
        """Check hosts (concurrently, each bounded by the timeout) and update the state.

        `only_not_ready` rechecks just the hosts that are not `ready` (the recheck loop);
        the default checks every host (startup). Hosts that left the configuration are
        dropped. Listeners run for every host that became `ready`.
        """
        hosts = list(self._hosts())
        targets = [h for h in hosts if not (only_not_ready and self.is_ready(h.name))]
        results = await asyncio.gather(*(self._check_one(host) for host in targets))
        for host in hosts:
            self._configs[host.name] = host
        newly_ready: list[str] = []
        for host, status in zip(targets, results, strict=True):
            previous = self._status.get(host.name)
            self._status[host.name] = status
            if status.state is HostState.READY and (
                previous is None or previous.state is not HostState.READY
            ):
                newly_ready.append(host.name)
            if previous != status:
                logger.info(
                    "host availability changed",
                    extra={"host": host.name, "state": status.state, "reason": status.reason},
                )
        configured = {host.name for host in hosts}
        for gone in set(self._status) - configured:
            del self._status[gone]
            self._configs.pop(gone, None)
        self.last_check_at = datetime.now(UTC)
        for name in newly_ready:
            for listener in self._listeners:
                try:
                    await listener(name)
                except Exception:
                    logger.exception("ready listener failed", extra={"host": name})

    async def run_recheck(self, interval: float) -> None:
        """Recheck every host that is not `ready` every `interval` seconds, until cancelled.

        A `ready` host is left alone: the checks cost a GitHub call and the spec asks only
        for rechecking failed hosts. An `unregistered` host becomes `ready` or `unavailable`
        here once the host list (see `HostsProvider`) carries its App.
        """
        while True:
            await asyncio.sleep(interval)
            try:
                await self.check_all(only_not_ready=True)
            except Exception:
                logger.exception("host recheck failed")
