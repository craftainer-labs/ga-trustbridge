"""A disabled tenant's webhooks are stored, acknowledged and not handled (ADR 0033 invariant 19)."""

import json
from datetime import timedelta

from tests.trustbridge.worker.harness import Harness, key
from trustbridge.github.handlers import EventContext


class SetGate:
    """A `TenantGate` over a set of disabled `(host, installation_id)` pairs."""

    def __init__(self, *disabled: tuple[str, int]) -> None:
        """Start with `disabled` tenants."""
        self.off = set(disabled)
        self.asked: list[tuple[str, int]] = []

    async def disabled(self, host: str, installation_id: int) -> bool:
        """True when the pair is in the set."""
        self.asked.append((host, installation_id))
        return (host, installation_id) in self.off


def _body(**payload: object) -> bytes:
    return json.dumps(payload).encode()


async def test_a_disabled_tenants_delivery_is_deferred_not_handled_nor_counted() -> None:
    """Acknowledged and left pending; no failure; picked up again after the tenant is enabled."""
    seen: list[str] = []

    async def handler(ctx: EventContext) -> None:
        seen.append(ctx.delivery_id)

    gate = SetGate(("ok", 7))
    h = await Harness({"push": handler}, gate=gate, disabled_tenant_retry_seconds=20).start()
    await h.add("d1")
    await h.worker.run_once()
    row = await h.store.get(key("d1"))
    assert row is not None
    assert (row.status, row.attempts) == ("pending", 0)
    assert row.next_attempt_at == h.clock() + timedelta(seconds=20)
    assert seen == []
    assert (await h.valkey.xpending("tb:events", "tb-workers"))["pending"] == 0
    # still disabled after the delay: deferred again, still not handled or counted
    h.advance(21)
    await h.relay.resweep(0)
    await h.worker.run_once()
    assert seen == []
    # enabled: the next pass handles it
    gate.off.clear()
    h.advance(21)
    await h.relay.resweep(0)
    await h.worker.run_once()
    row = await h.store.get(key("d1"))
    assert row is not None
    assert (row.status, row.attempts) == ("processed", 0)
    assert seen == ["d1"]


async def test_another_tenants_delivery_is_handled_while_one_is_disabled() -> None:
    """The gate is per installation."""
    seen: list[str] = []

    async def handler(ctx: EventContext) -> None:
        seen.append(ctx.delivery_id)

    h = await Harness({"push": handler}, gate=SetGate(("ok", 7))).start()
    await h.add("other", body=_body(installation={"id": 8}))
    await h.worker.run_once()
    assert seen == ["other"]


async def test_a_payload_without_an_installation_id_is_not_gated() -> None:
    """No installation, a non-object one or a non-integer id: the gate is not asked."""
    seen: list[str] = []

    async def handler(ctx: EventContext) -> None:
        seen.append(ctx.delivery_id)

    gate = SetGate(("ok", 7))
    h = await Harness({"push": handler}, gate=gate).start()
    await h.add("none", body=_body(x=1))
    await h.add("text", body=_body(installation="7"))
    await h.add("flag", body=_body(installation={"id": True}))
    await h.add("string-id", body=_body(installation={"id": "7"}))
    await h.worker.run_once()
    assert sorted(seen) == ["flag", "none", "string-id", "text"]
    assert gate.asked == []
