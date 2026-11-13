"""The tenant reconcile requests with claim/ack semantics (NG5r, ADR 0028 invariants 9 and 10).

The api records a request with `SADD tb:reconcile <tenant_id>` (`ReconcileRequests.schedule`; a
set, so a repeat is one request). The reconciler drains it here:

1. **claim**: `SMOVE tb:reconcile tb:reconcile:inflight <tenant_id>`, atomic, so two replicas
   never claim the same member, and a crash leaves it in the inflight set, never nowhere. The
   claim time and a per-claim token go to the hash `tb:reconcile:claims`.
2. **ack**: `SREM` from inflight and `HDEL` of the claim, after the reconcile finished (or the
   request was dropped on purpose). A request made again while its claim is held is a new member
   of the pending set, so it is not lost by the ack.
3. **reclaim**: a member that stays inflight longer than the claim TTL is moved back to the pending
   set. A member with no claim entry (a crash between the `SMOVE` and the `HSET`) is stamped first
   and reclaimed one TTL later, so a claimer that is only slow to write its entry is not undone.
4. **release**: `SMOVE` back to pending, for a request that was claimed and deliberately not
   acted on.

A slow holder whose claim was reclaimed finds out at its ack: the member left the inflight set or
carries another token, `ack` returns False and the caller logs it. The reconcile itself is
idempotent (ADR 0029), so the double run is detected, not harmful. Residual (documented): between
the token check and the `HDEL` in `reclaim` a holder can ack and a new claim can start, which makes
that one new claim reclaimable early; the request is then processed twice, never lost. A crash
between the `SREM` and the `HDEL` of an ack leaves a claim entry for a request that is no longer
inflight; the next claim of that tenant overwrites it, so it never matters.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from redis.asyncio import Redis

from trustbridge.worker.ports import RECONCILE_SET

logger = logging.getLogger(__name__)
INFLIGHT_SET = "tb:reconcile:inflight"
CLAIMS_HASH = "tb:reconcile:claims"
_SEP = "|"


@dataclass(frozen=True, slots=True)
class Claim:
    """One claimed request: the tenant and the token that proves the claim is still ours."""

    tenant_id: UUID
    token: str


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

        A member that is not a UUID is removed from the set and logged (nothing the api writes).
        """
        claimed: list[Claim] = []
        for raw in await self._r.srandmember(RECONCILE_SET, limit) or []:
            member = _text(raw)
            if not await self._r.smove(RECONCILE_SET, INFLIGHT_SET, member):
                continue  # another replica claimed it first
            try:
                tenant_id = UUID(member)
            except ValueError:
                logger.warning("dropped a malformed reconcile request")
                await self._r.srem(INFLIGHT_SET, member)
                continue
            token = uuid4().hex
            await self._r.hset(CLAIMS_HASH, member, _stamp(now, token))
            claimed.append(Claim(tenant_id, token))
        return claimed

    async def _holds(self, claim: Claim) -> bool:
        stored = await self._r.hget(CLAIMS_HASH, str(claim.tenant_id))
        parsed = None if stored is None else _parse(_text(stored))
        return parsed is not None and parsed[1] == claim.token

    async def ack(self, claim: Claim) -> bool:
        """Remove the request from inflight. False when the claim was reclaimed meanwhile."""
        member = str(claim.tenant_id)
        if not await self._holds(claim):
            return False
        removed = await self._r.srem(INFLIGHT_SET, member)
        await self._r.hdel(CLAIMS_HASH, member)
        return bool(removed)

    async def release(self, claim: Claim) -> bool:
        """Put the request back to pending without acting on it. False when the claim was lost."""
        member = str(claim.tenant_id)
        if not await self._holds(claim):
            return False
        await self._r.hdel(CLAIMS_HASH, member)
        return bool(await self._r.smove(INFLIGHT_SET, RECONCILE_SET, member))

    async def reclaim(self, now: datetime, ttl_seconds: float) -> int:
        """Return requests inflight for longer than `ttl_seconds` to pending; returns the count."""
        reclaimed = 0
        entries = {_text(k): _text(v) for k, v in (await self._r.hgetall(CLAIMS_HASH)).items()}
        for raw in await self._r.smembers(INFLIGHT_SET):
            member = _text(raw)
            value = entries.get(member)
            parsed = None if value is None else _parse(value)
            if parsed is None:
                # no (or an unreadable) claim entry: stamp it once, reclaim a TTL from now
                await self._r.hsetnx(CLAIMS_HASH, member, _stamp(now, "unclaimed"))
                continue
            if now.timestamp() - parsed[0] < ttl_seconds:
                continue
            if _text(await self._r.hget(CLAIMS_HASH, member) or "") != value:
                continue  # claimed again since the listing
            await self._r.hdel(CLAIMS_HASH, member)
            if await self._r.smove(INFLIGHT_SET, RECONCILE_SET, member):
                reclaimed += 1
                logger.warning("reclaimed a stranded reconcile request", extra={"tenant": member})
        return reclaimed
