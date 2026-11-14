"""`AdapterPlatformStep` over a fake port (NG6h-5/5b, ADR 0034 step 2): order, flag, skips, pass."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

import pytest

from tests.trustbridge.deletion.fakes import TENANT, record
from trustbridge.deletion.model import (
    Lease,
    LeaseLost,
    NonRetryableError,
    RunBudgetExhausted,
    StepContext,
    StepOutcome,
)
from trustbridge.deletion.platform import (
    COUNT_CATEGORY,
    KEPT_REASON,
    MAX_TRIES_PER_PASS,
    OPERATOR_OWNED_CATEGORY,
    OPERATOR_OWNED_REASON,
    AdapterPlatformStep,
    PlatformStepHeldError,
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
    fail: dict[str, Exception] = field(default_factory=dict)
    tried: list[str] = field(default_factory=list)

    async def pending(self, tenant_id: UUID) -> Sequence[PlatformItem]:
        """The items still recorded."""
        assert tenant_id == TENANT
        return list(self.items)

    async def remove(self, tenant_id: UUID, deletion_id: UUID, item: PlatformItem) -> None:
        """Remove one item, or raise."""
        self.tried.append(item.key)
        if self.boom is not None:
            raise self.boom
        if item.key in self.fail:
            raise self.fail[item.key]
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


def _ctx(
    *,
    flag: bool,
    cursor: dict[str, int] | None = None,
    budget: int | None = None,
    attempts: int = 0,
) -> tuple[StepContext, list[int]]:
    rec = record(
        state="platform",
        delete_platform_resources=flag,
        step_cursor=cursor or {},
        attempts=attempts,
    )
    renewals: list[int] = []

    async def renew() -> None:
        if budget is not None and len(renewals) >= budget:
            raise RunBudgetExhausted
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


async def test_one_pass_removes_everything_in_the_ruled_order() -> None:
    """Credentials, webhooks, repositories, projects; the cursor counts removals."""
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
    assert [o.cursor for o in outcomes[:-1]] == [{"removed": 5}]
    assert outcomes[0].counts == {
        "platform_access_grants": 2,
        "platform_webhooks": 1,
        "platform_repositories": 1,
        "platform_projects": 1,
    }
    assert outcomes[-1].done and outcomes[-1].counts == {} and outcomes[-1].skipped == []
    assert len(renewals) == 5


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
    port.items.remove(_item("credential", key="a"))  # removed, but the engine never saved it
    outcomes, _ = await _drain(step, flag=False)
    assert [i.key for _, _, i in port.removed] == ["b"]
    assert outcomes[-1].done and port.items == []


async def test_a_failing_head_does_not_hold_the_items_behind_it() -> None:
    """The failed item is skipped for the pass; the tail is removed and progress is reported."""
    port = FakePort(
        [_item("credential", "off", "a"), _item("credential", key="b"), _item("webhook", key="w")],
        fail={"a": RuntimeError("adapter off")},
    )
    step = AdapterPlatformStep(port, FakeNames())
    out = await step.run(_ctx(flag=False, cursor={"removed": 3})[0])
    assert port.tried == ["a", "b", "w"]  # each item once: no retry of `a` within the pass
    assert not out.done and out.cursor == {"removed": 5}
    assert out.counts == {"platform_access_grants": 1, "platform_webhooks": 1}
    assert port.items == [_item("credential", "off", "a")]


async def test_a_pass_where_nothing_is_removed_holds_with_type_only_diagnostics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """All failed: the transient error for the engine; no key, message or value leaks."""
    port = FakePort(
        [_item("credential", key="k-secret"), _item("webhook", key="w")],
        fail={"k-secret": RuntimeError("tok=hunter2"), "w": ValueError("tok=hunter2")},
    )
    with caplog.at_level(logging.WARNING), pytest.raises(PlatformStepHeldError) as held:
        await AdapterPlatformStep(port, FakeNames()).run(_ctx(flag=False)[0])
    assert str(held.value) == "2 of 2 removable items failed (RuntimeError x1, ValueError x1)"
    assert "hunter2" not in caplog.text and "k-secret" not in caplog.text
    assert "RuntimeError" in caplog.text
    assert len(port.items) == 2


async def test_held_pass_leaves_the_cursor_unchanged_so_attempts_keep_counting() -> None:
    """Progress (a changed cursor) is what resets the engine's attempts; a hold gives none."""
    port = FakePort([_item("credential", key="a")], boom=RuntimeError("down"))
    ctx, _ = _ctx(flag=False, cursor={"removed": 2})
    with pytest.raises(PlatformStepHeldError):
        await AdapterPlatformStep(port, FakeNames()).run(ctx)
    assert ctx.record.step_cursor == {"removed": 2}  # nothing returned, nothing to save
    port.boom = None
    out = await AdapterPlatformStep(port, FakeNames()).run(
        _ctx(flag=False, cursor={"removed": 2})[0]
    )
    assert out.cursor == {"removed": 3}


async def test_a_project_is_not_tried_while_one_of_its_repositories_remains() -> None:
    """A failing repository keeps its project; another project's repositories do not matter."""
    port = FakePort(
        [
            _item("repository", key="p1/r"),
            _item("repository", key="p2/r"),
            _item("project", key="p1"),
            _item("project", key="p2"),
            _item("project", key="p"),  # `p` is not a prefix match for `p1/r`
        ],
        fail={"p1/r": RuntimeError("x")},
    )
    out = await AdapterPlatformStep(port, FakeNames()).run(_ctx(flag=True)[0])
    assert port.tried == ["p1/r", "p2/r", "p2", "p"]
    assert out.counts == {"platform_repositories": 1, "platform_projects": 2}
    assert {i.key for i in port.items} == {"p1/r", "p1"}


async def test_only_failing_repositories_hold_with_their_project_untried() -> None:
    """The project is never tried, so it does not count as a failure."""
    port = FakePort(
        [_item("repository", key="p/r"), _item("project", key="p")],
        fail={"p/r": RuntimeError("x")},
    )
    with pytest.raises(PlatformStepHeldError, match="1 of 2 removable"):
        await AdapterPlatformStep(port, FakeNames()).run(_ctx(flag=True)[0])
    assert port.tried == ["p/r"]


async def test_budget_exhaustion_mid_pass_reports_what_was_removed() -> None:
    """The removed items are saved; the engine's next renew stops the run."""
    port = FakePort([_item("credential", key="a"), _item("credential", key="b")])
    out = await AdapterPlatformStep(port, FakeNames()).run(_ctx(flag=False, budget=1)[0])
    assert out.cursor == {"removed": 1} and out.counts == {"platform_access_grants": 1}
    assert port.tried == ["a"]


async def test_budget_exhaustion_before_any_removal_propagates() -> None:
    """Nothing to report: the engine releases the lease."""
    port = FakePort([_item("credential")])
    with pytest.raises(RunBudgetExhausted):
        await AdapterPlatformStep(port, FakeNames()).run(_ctx(flag=False, budget=0)[0])
    assert port.tried == []


async def test_a_lost_lease_is_not_an_item_failure() -> None:
    """`LeaseLost` from the port stops the pass at once."""
    port = FakePort([_item("credential", key="a"), _item("credential", key="b")], boom=LeaseLost())
    with pytest.raises(LeaseLost):
        await AdapterPlatformStep(port, FakeNames()).run(_ctx(flag=False)[0])
    assert port.tried == ["a"]


async def test_a_budget_error_from_the_port_is_not_an_item_failure() -> None:
    """`RunBudgetExhausted` raised by a removal propagates as it does from `renew`."""
    port = FakePort([_item("credential")], boom=RunBudgetExhausted())
    with pytest.raises(RunBudgetExhausted):
        await AdapterPlatformStep(port, FakeNames()).run(_ctx(flag=False)[0])


async def test_a_pass_is_bounded_in_items() -> None:
    """At most `MAX_TRIES_PER_PASS` tries; the rest wait for the next pass."""
    port = FakePort([_item("credential", key=f"k{n}") for n in range(MAX_TRIES_PER_PASS + 5)])
    out = await AdapterPlatformStep(port, FakeNames()).run(_ctx(flag=False)[0])
    assert out.cursor == {"removed": MAX_TRIES_PER_PASS} and len(port.items) == 5
    assert not out.done


async def test_stuck_head_items_do_not_starve_the_ones_behind_across_retries() -> None:
    """Budget spent on failing head items: hold (attempt counted), and the next start rotates."""
    items = [_item("credential", key=k) for k in ("a", "b", "c", "d")]
    port = FakePort(list(items), fail={"a": RuntimeError("t"), "b": RuntimeError("t")})
    step = AdapterPlatformStep(port, FakeNames())
    with pytest.raises(PlatformStepHeldError):  # attempt 0: the budget goes on `a` and `b`
        await step.run(_ctx(flag=False, budget=2, attempts=0)[0])
    assert port.tried == ["a", "b"] and port.removed == []
    out = await step.run(_ctx(flag=False, budget=2, attempts=1)[0])  # attempt 1 starts at `b`
    assert port.tried == ["a", "b", "b", "c"]
    assert out.counts == {"platform_access_grants": 1} and out.cursor == {"removed": 1}


async def test_the_start_rotates_by_attempts_and_wraps_in_order() -> None:
    """Start at `attempts % len`; the rest follow in order, wrapping."""
    keys = ["a", "b", "c"]
    port = FakePort([_item("credential", key=k) for k in keys], boom=RuntimeError("x"))
    with pytest.raises(PlatformStepHeldError):
        await AdapterPlatformStep(port, FakeNames()).run(_ctx(flag=False, attempts=4)[0])
    assert port.tried == ["b", "c", "a"]


async def test_budget_spent_on_failures_holds_with_type_only_diagnostics() -> None:
    """Failures and no removal when the budget ends: a held error, not a silent release."""
    port = FakePort(
        [_item("credential", key="s3cret"), _item("credential", key="b")], boom=KeyError("s3cret")
    )
    with pytest.raises(PlatformStepHeldError) as held:
        await AdapterPlatformStep(port, FakeNames()).run(_ctx(flag=False, budget=1)[0])
    assert str(held.value) == "1 of 2 removable items failed (KeyError x1)"


async def test_a_held_pass_does_not_depend_on_a_project_prefix_edge() -> None:
    """A project key that is a prefix of another project's repository key stays independent."""
    port = FakePort(
        [
            _item("repository", "other", "p/r"),  # a different adapter's repository
            _item("project", "harbor", "p"),
        ],
        fail={"p/r": RuntimeError("x")},
    )
    out = await AdapterPlatformStep(port, FakeNames()).run(_ctx(flag=True)[0])
    assert out.counts == {"platform_projects": 1}


async def test_a_budget_error_from_a_removal_after_removals_reports_them() -> None:
    """What the pass already removed is saved, as it is when `renew` ends the pass."""

    class _Port(FakePort):
        async def remove(self, tenant_id: UUID, deletion_id: UUID, item: PlatformItem) -> None:
            if item.key == "b":
                raise RunBudgetExhausted
            await super().remove(tenant_id, deletion_id, item)

    port = _Port([_item("credential", key="a"), _item("credential", key="b")])
    out = await AdapterPlatformStep(port, FakeNames()).run(_ctx(flag=False)[0])
    assert out.cursor == {"removed": 1} and out.counts == {"platform_access_grants": 1}


async def test_a_non_retryable_error_from_the_port_is_not_an_item_failure() -> None:
    """The engine decides what a `NonRetryableError` means; the pass does not swallow it."""
    port = FakePort([_item("credential")], boom=NonRetryableError("code"))
    with pytest.raises(NonRetryableError):
        await AdapterPlatformStep(port, FakeNames()).run(_ctx(flag=False)[0])


async def test_a_project_that_waited_for_its_repository_is_tried_in_the_same_pass() -> None:
    """A rotated start can put a project before its repository: a second sweep takes it."""
    port = FakePort([_item("repository", key="p/r"), _item("project", key="p")])
    out = await AdapterPlatformStep(port, FakeNames()).run(_ctx(flag=True, attempts=1)[0])
    assert port.tried == ["p/r", "p"]
    assert out.counts == {"platform_repositories": 1, "platform_projects": 1}
