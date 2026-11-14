"""`AdapterPlatformStep` over a fake port (NG6h-5, ADR 0034 step 2): order, flag, skips, errors."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

import pytest

from tests.trustbridge.deletion.fakes import TENANT, record
from trustbridge.deletion.model import Lease, StepContext, StepOutcome
from trustbridge.deletion.platform import (
    COUNT_CATEGORY,
    KEPT_REASON,
    OPERATOR_OWNED_CATEGORY,
    OPERATOR_OWNED_REASON,
    AdapterPlatformStep,
)
from trustbridge.deletion.ports import PlatformItem


def _item(kind: str, adapter: str = "harbor", key: str = "k") -> PlatformItem:
    return PlatformItem(kind, adapter, key)  # type: ignore[arg-type]


@dataclass
class FakePort:
    """Holds recorded items; `remove` forgets the item unless told to fail."""

    items: list[PlatformItem]
    removed: list[tuple[UUID, UUID, PlatformItem]] = field(default_factory=list)
    boom: Exception | None = None

    async def pending(self, tenant_id: UUID) -> Sequence[PlatformItem]:
        """The items still recorded."""
        assert tenant_id == TENANT
        return list(self.items)

    async def remove(self, tenant_id: UUID, deletion_id: UUID, item: PlatformItem) -> None:
        """Remove one item, or raise."""
        if self.boom is not None:
            raise self.boom
        self.removed.append((tenant_id, deletion_id, item))
        self.items.remove(item)


class FakeNames:
    """Connection names, and a record of what was asked."""

    def __init__(self, names: Sequence[str] = ()) -> None:
        """Start with `names`."""
        self._names = list(names)
        self.asked: list[UUID] = []

    async def names(self, tenant_id: UUID) -> Sequence[str]:
        """The adapter names."""
        self.asked.append(tenant_id)
        return self._names


def _ctx(*, flag: bool, cursor: dict[str, int] | None = None) -> tuple[StepContext, list[int]]:
    rec = record(state="platform", delete_platform_resources=flag, step_cursor=cursor or {})
    renewals: list[int] = []

    async def renew() -> None:
        renewals.append(1)

    return StepContext(rec, Lease(rec.id, "h", 1), renew), renewals


async def _drain(step: AdapterPlatformStep, *, flag: bool) -> tuple[list[StepOutcome], list[int]]:
    """Run passes the way the engine does: carry the cursor until `done`."""
    outcomes: list[StepOutcome] = []
    renewals: list[int] = []
    cursor: dict[str, int] = {}
    while True:
        ctx, renewed = _ctx(flag=flag, cursor=cursor)
        out = await step.run(ctx)
        renewals += renewed
        outcomes.append(out)
        if out.done:
            return outcomes, renewals
        cursor = dict(out.cursor or {})


async def test_removes_one_item_per_pass_in_the_ruled_order() -> None:
    """Credentials, then webhooks, repositories, projects; each pass carries a new cursor."""
    port = FakePort(
        [
            _item("project", key="p"),
            _item("repository", key="p/r"),
            _item("webhook", key="w"),
            _item("credential", "argocd", "s1"),
            _item("credential", "harbor", "s2"),
        ]
    )
    step = AdapterPlatformStep(port, FakeNames())
    outcomes, renewals = await _drain(step, flag=True)
    assert [i.kind for _, _, i in port.removed] == [
        "credential",
        "credential",
        "webhook",
        "repository",
        "project",
    ]
    assert [o.cursor for o in outcomes[:-1]] == [{"removed": n} for n in range(1, 6)]
    assert outcomes[-1].done and outcomes[-1].counts == {} and outcomes[-1].skipped == []
    assert len(renewals) == 5
    total: dict[str, int] = {}
    for out in outcomes:
        for key, value in out.counts.items():
            total[key] = total.get(key, 0) + value
    assert total == {
        "platform_access_grants": 2,
        "platform_webhooks": 1,
        "platform_repositories": 1,
        "platform_projects": 1,
    }


async def test_every_removal_carries_the_deletions_own_origin() -> None:
    """The port gets the tenant id and the deletion id (the gate's `deletion_id` origin)."""
    port = FakePort([_item("credential")])
    ctx, _ = _ctx(flag=False)
    await AdapterPlatformStep(port, FakeNames()).run(ctx)
    assert port.removed == [(TENANT, ctx.record.id, _item("credential"))]


async def test_without_the_flag_projects_and_repositories_are_kept_and_recorded() -> None:
    """User decision 1: credentials and webhooks go; repositories and projects are skipped."""
    port = FakePort(
        [
            _item("project", key="p1"),
            _item("project", key="p2"),
            _item("repository", key="p1/r"),
            _item("webhook", key="w"),
            _item("credential"),
        ]
    )
    outcomes, _ = await _drain(AdapterPlatformStep(port, FakeNames()), flag=False)
    assert {i.kind for _, _, i in port.removed} == {"credential", "webhook"}
    assert {i.kind for i in port.items} == {"project", "repository"}
    assert outcomes[-1].skipped == [
        {"category": COUNT_CATEGORY["repository"], "reason": KEPT_REASON, "count": 1},
        {"category": COUNT_CATEGORY["project"], "reason": KEPT_REASON, "count": 2},
    ]


async def test_operator_owned_connection_names_are_recorded_once_at_the_end() -> None:
    """Names only, in the last pass only (a partial pass appends nothing to `skipped`)."""
    names = FakeNames(["argocd", "harbor"])
    port = FakePort([_item("credential")])
    outcomes, _ = await _drain(AdapterPlatformStep(port, names), flag=False)
    assert outcomes[0].skipped == () and not outcomes[0].done
    assert outcomes[-1].skipped == [
        {
            "category": OPERATOR_OWNED_CATEGORY,
            "reason": OPERATOR_OWNED_REASON,
            "adapter": name,
            "count": 1,
        }
        for name in ("argocd", "harbor")
    ]
    assert names.asked == [TENANT]


async def test_an_empty_tenant_completes_at_once() -> None:
    """Nothing recorded: one pass, done, nothing skipped."""
    out = await AdapterPlatformStep(FakePort([]), FakeNames()).run(_ctx(flag=True)[0])
    assert out.done and out.counts == {} and out.skipped == []


async def test_a_crash_after_a_removal_converges() -> None:
    """The work list is re-read each pass: an item already gone is not removed again."""
    port = FakePort([_item("credential", key="a"), _item("credential", key="b")])
    step = AdapterPlatformStep(port, FakeNames())
    await step.run(_ctx(flag=False)[0])  # removed `a`; the engine never saved the cursor
    outcomes, _ = await _drain(step, flag=False)
    assert [i.key for _, _, i in port.removed] == ["a", "b"]
    assert outcomes[-1].done and port.items == []


async def test_a_failing_removal_propagates_and_changes_nothing() -> None:
    """Any error is the engine's to retry; the step holds in `platform`."""
    port = FakePort([_item("credential")], boom=RuntimeError("platform down"))
    with pytest.raises(RuntimeError):
        await AdapterPlatformStep(port, FakeNames()).run(_ctx(flag=False)[0])
    assert port.items == [_item("credential")]
