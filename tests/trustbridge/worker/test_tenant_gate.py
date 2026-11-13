"""A disabled tenant's webhooks are stored, acknowledged and not handled (ADR 0033 invariant 19)."""

import json
import logging
from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from tests.trustbridge.worker.harness import Harness, key
from trustbridge.github.handlers import EventContext
from trustbridge.tenancy.standing import TenantStanding


class SetGate:
    """A `TenantGate` over sets of disabled, deleting and tombstoned `(host, installation_id)`."""

    def __init__(self, *disabled: tuple[str, int]) -> None:
        """Start with `disabled` tenants; every installation has a tenant except tombstones."""
        self.off = set(disabled)
        self.deleting: set[tuple[str, int]] = set()
        self.tombstones: set[tuple[str, int]] = set()
        self.asked: list[tuple[str, int]] = []
        self.ids: dict[tuple[str, int], UUID] = {}

    async def standing(self, host: str, installation_id: int) -> TenantStanding:
        """The standing of the pair."""
        pair = (host, installation_id)
        self.asked.append(pair)
        if pair in self.tombstones:
            return TenantStanding(refusal="tombstone")
        tenant_id = self.ids.setdefault(pair, uuid4())
        return TenantStanding(
            tenant_id,
            disabled=pair in self.off or pair in self.deleting,
            refusal="deleting" if pair in self.deleting else None,
        )


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


async def test_the_tenant_is_recorded_on_the_delivery_before_the_deferral() -> None:
    """ADR 0034: a deferred delivery already names its tenant, so a deletion can find it."""
    gate = SetGate(("ok", 7))
    h = await Harness({"push": _noop}, gate=gate).start()
    await h.add("d1")
    await h.worker.run_once()
    row = await h.store.get(key("d1"))
    assert row is not None
    assert (row.status, row.tenant_id) == ("pending", gate.ids[("ok", 7)])


async def test_a_handled_delivery_keeps_its_tenant_and_other_tenants_are_recorded_too() -> None:
    """The id is stored whether the delivery is handled or deferred."""
    gate = SetGate(("ok", 7))
    h = await Harness({"push": _noop}, gate=gate).start()
    await h.add("other", body=_body(installation={"id": 8}))
    await h.worker.run_once()
    row = await h.store.get(key("other"))
    assert row is not None
    assert (row.status, row.tenant_id) == ("processed", gate.ids[("ok", 8)])


async def test_a_delivery_for_a_tenant_being_deleted_is_deferred_not_handled_nor_counted() -> None:
    """A deletion in progress is a no-op for the delivery: it stays pending, uncounted."""
    seen: list[str] = []

    async def handler(ctx: EventContext) -> None:
        seen.append(ctx.delivery_id)

    gate = SetGate()
    gate.deleting.add(("ok", 7))
    h = await Harness({"push": handler}, gate=gate, disabled_tenant_retry_seconds=20).start()
    await h.add("d1")
    await h.worker.run_once()
    row = await h.store.get(key("d1"))
    assert row is not None
    assert (row.status, row.attempts, row.tenant_id) == ("pending", 0, gate.ids[("ok", 7)])
    # four times the disabled-tenant delay: `enable` does not end a deletion
    assert row.next_attempt_at == h.clock() + timedelta(seconds=80)
    assert seen == []


async def test_the_deletion_deferral_logs_at_info_once_then_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The first deferral of a delivery is at info, the later passes at debug."""
    gate = SetGate()
    gate.deleting.add(("ok", 7))
    h = await Harness({"push": _noop}, gate=gate, disabled_tenant_retry_seconds=20).start()
    await h.add("d1")
    message = "deferring push delivery: tenant deletion in progress"
    with caplog.at_level(logging.DEBUG, logger="trustbridge.worker.worker"):
        await h.worker.run_once()
        h.advance(81)
        await h.relay.resweep(0)
        await h.worker.run_once()
    levels = [r.levelno for r in caplog.records if r.getMessage() == message]
    assert levels == [logging.INFO, logging.DEBUG]


async def test_a_tombstoned_installations_delivery_is_acknowledged_and_dropped() -> None:
    """No handler runs and no tenant is recorded; the delivery is processed, never retried."""
    seen: list[str] = []

    async def handler(ctx: EventContext) -> None:
        seen.append(ctx.delivery_id)

    gate = SetGate()
    gate.tombstones.add(("ok", 7))
    h = await Harness({"push": handler}, gate=gate).start()
    await h.add("d1")
    await h.worker.run_once()
    row = await h.store.get(key("d1"))
    assert row is not None
    assert (row.status, row.attempts, row.tenant_id) == ("processed", 0, None)
    assert seen == []
    assert (await h.valkey.xpending("tb:events", "tb-workers"))["pending"] == 0


async def _noop(ctx: EventContext) -> None:
    del ctx
