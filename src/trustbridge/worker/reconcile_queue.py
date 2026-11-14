"""The tenant reconcile requests with claim/ack semantics (NG5r, ADR 0028 invariants 9 and 10).

The api records a request with `SADD tb:reconcile <tenant_id>` (`ReconcileRequests.schedule`; a
set, so a repeat is one request). The reconciler drains it here. Every step that changes two keys
together is one Lua script (one atomic server-side step, so there is no window between the keys
and a stale holder cannot undo a newer claim):

1. **claim**: `SMOVE tb:reconcile tb:reconcile:inflight <tenant_id>` and `HSET` of the claim
   (time and a per-claim token) in `tb:reconcile:claims`. Two replicas never claim the same member,
   and a crash leaves it in the inflight set, never nowhere.
2. **ack**: only if the stored token is still the caller's: `HDEL` of the claim and `SREM` from
   inflight. A request made again while its claim is held is a new member of the pending set, so
   it is not lost by the ack.
3. **reclaim**: a member that stays inflight longer than the claim TTL is moved back to the pending
   set, only if its claim entry is still the one that was listed (`HDEL` and `SMOVE` together). A
   member with no claim entry (written by nothing here, but possible after a manual edit or an
   older release) is stamped first and reclaimed one TTL later.
4. **release**: the same token check, then `HDEL` and `SMOVE` back to pending, for a request that
   was claimed and deliberately not acted on.
5. **extend**: the same token check, then the claim's time is re-stamped (the token is kept). A
   running body calls it at each heartbeat (a visibility-timeout extension), so a reclaim only
   takes a request whose holder stopped heartbeating. A False answer means the claim is gone.

The attempts of a request live in a separate hash, `tb:reconcile:attempts` (a reclaim removes the
claim entry, so the count cannot live there): `begin_attempt` adds one before the body runs and
`clear_attempts` removes the count when the request is acknowledged. The reconciler drops a request
whose count passes `TENANT_RECONCILE_MAX_ATTEMPTS` (poison handling, ADR 0029).

A slow holder whose claim was reclaimed finds out at its ack: the claim entry is gone or carries
another token, so `ack` returns False and the caller logs it. The reconcile itself is idempotent
(ADR 0029), so the double run is detected, not harmful. A request is never lost: it is always in
the pending or the inflight set until a holder that still owns the claim acks it. Residual
(documented, not harmful): when a holder acks after its claim was reclaimed but before anyone
re-claimed it, the request stays pending and runs once more.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from redis.asyncio import Redis

from trustbridge.worker.ports import RECONCILE_SET

logger = logging.getLogger(__name__)
INFLIGHT_SET = "tb:reconcile:inflight"
CLAIMS_HASH = "tb:reconcile:claims"
ATTEMPTS_HASH = "tb:reconcile:attempts"
_SEP = "|"

# KEYS: pending set, inflight set, claims hash. ARGV: see each script. The token is the text after
# the first `|` of a claim entry (`<epoch>|<token>`).
_CLAIM = """
if redis.call('SMOVE', KEYS[1], KEYS[2], ARGV[1]) == 1 then
    redis.call('HSET', KEYS[3], ARGV[1], ARGV[2])
    return 1
end
return 0
"""
# ARGV: member, token
_ACK = """
local v = redis.call('HGET', KEYS[3], ARGV[1])
if not v or string.match(v, '^[^|]*|(.*)$') ~= ARGV[2] then return 0 end
redis.call('HDEL', KEYS[3], ARGV[1])
return redis.call('SREM', KEYS[2], ARGV[1])
"""
# ARGV: member, token
_RELEASE = """
local v = redis.call('HGET', KEYS[3], ARGV[1])
if not v or string.match(v, '^[^|]*|(.*)$') ~= ARGV[2] then return 0 end
redis.call('HDEL', KEYS[3], ARGV[1])
return redis.call('SMOVE', KEYS[2], KEYS[1], ARGV[1])
"""
# ARGV: member, token, the new stamp (the same token, a new time)
_EXTEND = """
local v = redis.call('HGET', KEYS[3], ARGV[1])
if not v or string.match(v, '^[^|]*|(.*)$') ~= ARGV[2] then return 0 end
redis.call('HSET', KEYS[3], ARGV[1], ARGV[3])
return 1
"""
# ARGV: member, a stamp, the entry as listed ('' for none): stamps a member that is still inflight
# and whose entry is still the listed one (absent, or unreadable)
_STAMP = """
if redis.call('SISMEMBER', KEYS[2], ARGV[1]) ~= 1 then return 0 end
local v = redis.call('HGET', KEYS[3], ARGV[1])
if (v or '') ~= ARGV[3] then return 0 end
redis.call('HSET', KEYS[3], ARGV[1], ARGV[2])
return 1
"""
# ARGV: member, the claim entry as listed
_RECLAIM = """
if redis.call('HGET', KEYS[3], ARGV[1]) ~= ARGV[2] then return 0 end
redis.call('HDEL', KEYS[3], ARGV[1])
return redis.call('SMOVE', KEYS[2], KEYS[1], ARGV[1])
"""


@dataclass(frozen=True, slots=True)
class Claim:
    """One claimed request: the tenant and the token that proves the claim is still ours."""

    tenant_id: UUID
    token: str


class ClaimLostError(Exception):
    """A running reconcile found its claim reclaimed (or acknowledged) by someone else."""


Extend = Callable[[], Awaitable[None]]
"""Re-stamps the running claim; raises `ClaimLostError` when it is gone (before each write step)."""


def _text(value: str | bytes | int) -> str:
    """A reply as text (the client decodes responses; the stubs allow bytes and int too)."""
    return value.decode() if isinstance(value, bytes) else str(value)


def _stamp(now: datetime, token: str) -> str:
    return f"{now.timestamp():.3f}{_SEP}{token}"


def _parse(value: str) -> tuple[float, str] | None:
    when, _, token = value.partition(_SEP)
    try:
        return float(when), token
    except ValueError:
        return None


class ReconcileQueue:
    """Claim, ack, release and reclaim over the Valkey sets (decoded responses)."""

    def __init__(self, redis: Redis) -> None:
        """Bind the Valkey client (`decode_responses=True`)."""
        self._r = redis

    async def pending(self) -> int:
        """Number of requests waiting to be claimed."""
        return int(await self._r.scard(RECONCILE_SET))

    async def inflight(self) -> int:
        """Number of claimed, unacknowledged requests."""
        return int(await self._r.scard(INFLIGHT_SET))

    async def claim(self, now: datetime, limit: int) -> list[Claim]:
        """Move up to `limit` requests to the inflight set and stamp each claim.

        A member that is not a UUID is removed from the set and logged; one in a non-canonical
        spelling is re-queued as the canonical text (the api writes only that).
        """
        claimed: list[Claim] = []
        for raw in await self._r.srandmember(RECONCILE_SET, limit) or []:
            member = _text(raw)
            try:
                tenant_id = UUID(member)
            except ValueError:
                logger.warning("dropped a malformed reconcile request")
                await self._r.srem(RECONCILE_SET, member)
                continue
            if str(tenant_id) != member:
                # a valid id in another spelling (upper case, braces) would key its claim wrongly:
                # re-queue it canonically, the add first so the request is never in neither
                logger.warning("re-queued a reconcile request under its canonical id")
                await self._r.sadd(RECONCILE_SET, str(tenant_id))
                await self._r.srem(RECONCILE_SET, member)
                continue
            token = uuid4().hex
            if await self._run(_CLAIM, member, _stamp(now, token)):
                claimed.append(Claim(tenant_id, token))
            # else: another replica claimed it first
        return claimed

    async def _run(self, script: str, *args: str) -> int:
        """Run one of the scripts over the three keys; its integer reply."""
        keys = (RECONCILE_SET, INFLIGHT_SET, CLAIMS_HASH)
        return int(await self._r.eval(script, len(keys), *keys, *args))

    async def ack(self, claim: Claim) -> bool:
        """Remove the request from inflight. False when the claim was reclaimed meanwhile."""
        return bool(await self._run(_ACK, str(claim.tenant_id), claim.token))

    async def release(self, claim: Claim) -> bool:
        """Put the request back to pending without acting on it. False when the claim was lost."""
        return bool(await self._run(_RELEASE, str(claim.tenant_id), claim.token))

    async def extend(self, claim: Claim, now: datetime) -> bool:
        """Re-stamp the claim with `now`, only if its token is still ours; False when it is gone."""
        stamp = _stamp(now, claim.token)
        return bool(await self._run(_EXTEND, str(claim.tenant_id), claim.token, stamp))

    async def begin_attempt(self, claim: Claim) -> int:
        """Count one more run of the request's body; returns the number of runs so far."""
        return int(await self._r.hincrby(ATTEMPTS_HASH, str(claim.tenant_id), 1))

    async def clear_attempts(self, claim: Claim) -> None:
        """Forget the request's attempts (it was acknowledged)."""
        await self._r.hdel(ATTEMPTS_HASH, str(claim.tenant_id))

    async def reclaim(self, now: datetime, ttl_seconds: float) -> int:
        """Return requests inflight for longer than `ttl_seconds` to pending; returns the count."""
        reclaimed = 0
        entries = {_text(k): _text(v) for k, v in (await self._r.hgetall(CLAIMS_HASH)).items()}
        for raw in await self._r.smembers(INFLIGHT_SET):
            member = _text(raw)
            value = entries.get(member)
            parsed = None if value is None else _parse(value)
            if parsed is None or value is None:
                # no (or an unreadable) claim entry: stamp it, reclaim a TTL from now
                await self._run(_STAMP, member, _stamp(now, "unclaimed"), value or "")
                continue
            if now.timestamp() - parsed[0] < ttl_seconds:
                continue
            if await self._run(_RECLAIM, member, value):
                reclaimed += 1
                logger.warning("reclaimed a stranded reconcile request", extra={"tenant": member})
        return reclaimed
