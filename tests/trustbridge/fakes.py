"""In-memory fakes for the trustbridge unit tests: a stream-capable Valkey client and friends."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import SecretStr
from redis.exceptions import ResponseError

from trustbridge.github.hosts import GitHubHostConfig
from trustbridge.worker.reconciler import HookDelivery
from trustbridge.worker.secrets import SecretUnavailable


def id_key(entry_id: str) -> tuple[int, int]:
    """Sortable form of a stream id."""
    ms, _, seq = entry_id.partition("-")
    return int(ms), int(seq or 0)


@dataclass
class _Pending:
    consumer: str
    delivered_ms: int
    count: int = 1


@dataclass
class _Group:
    last_id: str = "0-0"
    pending: dict[str, _Pending] = field(default_factory=dict)


class FakeRedis:
    """The subset of redis-py's asyncio client that trustbridge.worker.queue/limiter use."""

    def __init__(self) -> None:
        """Start empty at t=1000 ms."""
        self.now_ms = 1000
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.groups: dict[tuple[str, str], _Group] = {}
        self.kv: dict[str, int] = {}
        self.sets: dict[str, set[str]] = {}
        self.expiries: dict[str, int] = {}
        self._last = (0, -1)
        self.down = False

    def advance(self, ms: int) -> None:
        """Move the fake clock."""
        self.now_ms += ms

    def _check(self) -> None:
        if self.down:
            raise ConnectionError("fake redis is down")

    def _new_id(self) -> str:
        ms, seq = self._last
        self._last = (self.now_ms, 0) if self.now_ms > ms else (ms, seq + 1)
        return f"{self._last[0]}-{self._last[1]}"

    def entries(self, name: str) -> list[tuple[str, dict[str, str]]]:
        """The stream's entries."""
        return list(self.streams.get(name, []))

    def ids_of(self, name: str) -> list[str]:
        """The `id` field (delivery id) of each entry in order."""
        return [fields["id"] for _, fields in self.entries(name)]

    async def sadd(self, name: str, *values: str) -> int:
        """Add members to a set."""
        self._check()
        members = self.sets.setdefault(name, set())
        before = len(members)
        members.update(values)
        return len(members) - before

    async def flushall(self) -> None:
        """Lose everything (restart without persistence)."""
        self.streams.clear()
        self.groups.clear()
        self.kv.clear()

    async def xgroup_create(
        self, name: str, groupname: str, id: str = "$", mkstream: bool = False  # noqa: A002
    ) -> bool:
        """Create a group; BUSYGROUP if it exists."""
        self._check()
        if name not in self.streams:
            if not mkstream:
                raise ResponseError("ERR no such key")
            self.streams[name] = []
        if (name, groupname) in self.groups:
            raise ResponseError("BUSYGROUP Consumer Group name already exists")
        self.groups[(name, groupname)] = _Group(last_id="0-0" if id == "0" else id)
        return True

    async def xadd(self, name: str, fields: dict[str, str]) -> str:
        """Append an entry."""
        self._check()
        entry_id = self._new_id()
        self.streams.setdefault(name, []).append((entry_id, dict(fields)))
        return entry_id

    async def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        count: int | None = None,
        block: int | None = None,
    ) -> list[Any]:
        """Deliver new entries (`>`) and add them to the pending list."""
        self._check()
        out = []
        for name in streams:
            group = self.groups[(name, groupname)]
            fresh = [e for e in self.streams[name] if id_key(e[0]) > id_key(group.last_id)]
            fresh = fresh[:count]
            for entry_id, _ in fresh:
                group.pending[entry_id] = _Pending(consumername, self.now_ms)
                group.last_id = entry_id
            if fresh:
                out.append([name, fresh])
        return out

    async def xautoclaim(  # noqa: PLR0913, PLR0917
        self,
        name: str,
        groupname: str,
        consumername: str,
        min_idle_time: int,
        start_id: str = "0-0",
        count: int | None = None,
    ) -> list[Any]:
        """Claim idle pending entries, counting a delivery."""
        self._check()
        group = self.groups[(name, groupname)]
        present = dict(self.streams[name])
        claimed = []
        for entry_id in sorted(group.pending, key=id_key):
            info = group.pending[entry_id]
            if id_key(entry_id) < id_key(start_id) or self.now_ms - info.delivered_ms < min_idle_time:
                continue
            if entry_id not in present:
                del group.pending[entry_id]
                continue
            info.consumer, info.delivered_ms, info.count = consumername, self.now_ms, info.count + 1
            claimed.append((entry_id, present[entry_id]))
        return ["0-0", claimed[:count], []]

    async def xpending_range(
        self, name: str, groupname: str, min: str, max: str, count: int  # noqa: A002
    ) -> list[dict[str, Any]]:
        """Pending entries between two ids (inclusive)."""
        self._check()
        group = self.groups[(name, groupname)]
        low = (0, 0) if min == "-" else id_key(min)
        high = (2**63, 0) if max == "+" else id_key(max)
        rows = [
            {
                "message_id": i,
                "consumer": p.consumer,
                "time_since_delivered": self.now_ms - p.delivered_ms,
                "times_delivered": p.count,
            }
            for i, p in sorted(group.pending.items(), key=lambda kv: id_key(kv[0]))
            if low <= id_key(i) <= high
        ]
        return rows[:count]

    async def xpending(self, name: str, groupname: str) -> dict[str, Any]:
        """The pending summary."""
        self._check()
        ids = sorted(self.groups[(name, groupname)].pending, key=id_key)
        return {
            "pending": len(ids),
            "min": ids[0] if ids else None,
            "max": ids[-1] if ids else None,
            "consumers": [],
        }

    async def xack(self, name: str, groupname: str, *ids: str) -> int:
        """Acknowledge entries."""
        self._check()
        group = self.groups[(name, groupname)]
        return sum(1 for i in ids if group.pending.pop(i, None) is not None)

    async def xinfo_groups(self, name: str) -> list[dict[str, Any]]:
        """Group info; ResponseError if the stream does not exist."""
        self._check()
        if name not in self.streams:
            raise ResponseError("ERR no such key")
        out = []
        for (stream, gname), group in self.groups.items():
            if stream != name:
                continue
            lag = sum(1 for i, _ in self.streams[name] if id_key(i) > id_key(group.last_id))
            out.append(
                {
                    "name": gname,
                    "consumers": 1,
                    "pending": len(group.pending),
                    "last-delivered-id": group.last_id,
                    "lag": lag,
                }
            )
        return out

    async def xtrim(
        self, name: str, maxlen: int | None = None, approximate: bool = True, minid: str | None = None
    ) -> int:
        """Remove entries with an id strictly below `minid`."""
        self._check()
        entries = self.streams.get(name, [])
        keep = [e for e in entries if minid is None or id_key(e[0]) >= id_key(minid)]
        self.streams[name] = keep
        return len(entries) - len(keep)

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        """A MULTI/EXEC-like pipeline for the limiter."""
        return FakePipeline(self)


class FakePipeline:
    """`INCR` + `EXPIRE NX`, applied atomically on `execute`."""

    def __init__(self, redis: FakeRedis) -> None:
        """Bind the fake."""
        self._r = redis
        self._ops: list[tuple[str, str, int]] = []

    async def __aenter__(self) -> FakePipeline:
        """Enter."""
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Exit."""

    def incr(self, key: str) -> None:
        """Queue INCR."""
        self._ops.append(("incr", key, 0))

    def expire(self, key: str, ttl: int, nx: bool = False) -> None:
        """Queue EXPIRE NX."""
        self._ops.append(("expire", key, ttl))

    async def execute(self) -> list[int]:
        """Run the queued commands."""
        self._r._check()
        out = []
        for op, key, ttl in self._ops:
            if op == "incr":
                self._r.kv[key] = self._r.kv.get(key, 0) + 1
                out.append(self._r.kv[key])
            else:
                first = key not in self._r.expiries
                self._r.expiries.setdefault(key, ttl)
                out.append(1 if first else 0)
        return out


class FakeSecretStore:
    """A `SecretReader` whose reads can be switched off."""

    def __init__(self, secrets: dict[str, str]) -> None:
        """Hold `{ref: value}`."""
        self.secrets = secrets
        self.down = False
        self.reads = 0

    async def read(self, ref: str) -> SecretStr:
        """Return the value or fail like an outage."""
        self.reads += 1
        if self.down or ref not in self.secrets:
            raise SecretUnavailable("down")
        return SecretStr(self.secrets[ref])


class FakeHostChecker:
    """`HostChecker` (the network checks): hosts in `failing` report the wrong App id."""

    def __init__(self, failing: set[str] | None = None) -> None:
        """`failing` host names fail the `GET /app` id match."""
        self.failing = failing or set()
        self.calls: list[str] = []

    async def get_app_id(self, host: GitHubHostConfig, jwt: str) -> int:
        """Record the check; answer the configured id, or a wrong one for a failing host."""
        del jwt
        self.calls.append(host.name)
        return -1 if host.name in self.failing else (host.app_id or 0)

    async def discovery_issuer(self, host: GitHubHostConfig) -> str:
        """Answer the configured issuer."""
        return host.oidc_issuer


class FakeHookClient:
    """GitHub's hook-delivery endpoints, in memory."""

    def __init__(self, deliveries: list[HookDelivery]) -> None:
        """`deliveries` newest first."""
        self.deliveries = deliveries
        self.redelivered: list[int] = []

    async def list_deliveries(
        self, host: GitHubHostConfig, *, since: datetime
    ) -> AsyncIterator[HookDelivery]:
        """Yield the attempts."""
        for item in self.deliveries:
            yield item

    async def redeliver(self, host: GitHubHostConfig, hook_delivery_id: int) -> None:
        """Record the redelivery."""
        self.redelivered.append(hook_delivery_id)


class Clock:
    """A settable UTC clock."""

    def __init__(self) -> None:
        """Start at a fixed instant."""
        self.now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        """The current fake time."""
        return self.now

    def advance(self, **kwargs: float) -> None:
        """Move forward."""
        self.now += timedelta(**kwargs)
