"""`GithubDeletionStep` over a fake port (NG6h-6, ADR 0034 step 3, B6 and B9)."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from tests.trustbridge.deletion.fakes import TENANT, record
from trustbridge.deletion.github import (
    CREATED_AT_SKEW,
    MAX_TRIES_PER_PASS,
    GithubDeletionStep,
    GithubStepHeldError,
)
from trustbridge.deletion.model import (
    Lease,
    LeaseLost,
    NonRetryableError,
    RunBudgetExhausted,
    StepContext,
    StepOutcome,
)
from trustbridge.deletion.ports import GithubOutput, TrackedEnvironment

ROW_TIME = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def _out(name: str, kind: str = "secret", repo: int | None = 10) -> GithubOutput:
    return GithubOutput("harbor", "scope", kind, name, repo, None)  # type: ignore[arg-type]


def _env(name: str, repo: int = 10) -> TrackedEnvironment:
    return TrackedEnvironment(repo, name, ROW_TIME)


@dataclass
class FakePort:
    """Recorded items and GitHub's state; removals forget the item unless told to fail."""

    outs: list[GithubOutput] = field(default_factory=list)
    envs: list[TrackedEnvironment] = field(default_factory=list)
    untracked: int = 0
    exists: bool = True
    exists_error: Exception | None = None
    github_created: dict[str, datetime | None] = field(default_factory=dict)
    fail: dict[str, Exception] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    released: list[str] = field(default_factory=list)

    async def installation_exists(self, host: str, installation_id: int) -> bool:
        """The App's presence."""
        self.calls.append("installation")
        assert (host, installation_id) == ("github.com", 42)
        if self.exists_error is not None:
            raise self.exists_error
        return self.exists

    async def outputs(self, tenant_id: UUID) -> Sequence[GithubOutput]:
        """The recorded outputs."""
        assert tenant_id == TENANT
        return list(self.outs)

    async def remove_output(self, tenant_id: UUID, output: GithubOutput) -> None:
        """Remove one, or raise."""
        self.calls.append(f"remove:{output.name}")
        if output.name in self.fail:
            raise self.fail[output.name]
        self.outs.remove(output)

    async def environments(self, tenant_id: UUID) -> Sequence[TrackedEnvironment]:
        """The tracked environments."""
        return list(self.envs)

    async def untracked_environments(self, tenant_id: UUID) -> int:
        """The untracked count."""
        return self.untracked

    async def environment_created_at(
        self, tenant_id: UUID, environment: TrackedEnvironment
    ) -> datetime | None:
        """GitHub's time (the row's own by default), or an error."""
        self.calls.append(f"get:{environment.name}")
        if environment.name in self.fail:
            raise self.fail[environment.name]
        return self.github_created.get(environment.name, ROW_TIME)

    async def delete_environment(self, tenant_id: UUID, environment: TrackedEnvironment) -> None:
        """Delete it."""
        self.deleted.append(environment.name)

    async def release_environment(self, tenant_id: UUID, environment: TrackedEnvironment) -> None:
        """Stop tracking it."""
        self.released.append(environment.name)
        self.envs.remove(environment)


def _ctx(
    *, cursor: dict[str, int] | None = None, attempts: int = 0, budget: int | None = None
) -> StepContext:
    rec = record(state="github", step_cursor=cursor or {}, attempts=attempts)
    renewals: list[int] = []

    async def renew() -> None:
        if budget is not None and len(renewals) >= budget:
            raise RunBudgetExhausted
        renewals.append(1)

    return StepContext(rec, Lease(rec.id, "h", 1), renew)


async def _drain(port: FakePort) -> list[StepOutcome]:
    """Run passes the way the engine does: carry the cursor until `done`."""
    step = GithubDeletionStep(port)
    outcomes: list[StepOutcome] = []
    cursor: dict[str, int] = {}
    while True:
        out = await step.run(_ctx(cursor=cursor))
        outcomes.append(out)
        if out.done:
            return outcomes
        cursor = dict(out.cursor or {})


async def test_outputs_then_environments_are_removed_and_the_last_pass_finishes() -> None:
    """Secrets and variables first, then tracked environments; the cursor counts what settled."""
    port = FakePort(
        outs=[_out("S1"), _out("V1", "variable", None)],
        envs=[_env("prod"), _env("staging")],
    )
    outcomes = await _drain(port)
    assert port.calls[:3] == ["installation", "remove:S1", "remove:V1"]
    assert port.deleted == ["prod", "staging"]
    assert port.released == ["prod", "staging"]
    first, last = outcomes
    assert first.counts == {"github_outputs_removed": 2, "github_environments_removed": 2}
    assert first.cursor == {"handled": 4} and not first.done
    assert last.done and not last.counts and not last.skipped


async def test_an_empty_tenant_completes_without_a_github_call() -> None:
    """Nothing recorded: no installation lookup, no skipped entry."""
    port = FakePort()
    out = await GithubDeletionStep(port).run(_ctx())
    assert out.done and not out.counts and not out.skipped
    assert port.calls == []


async def test_a_gone_installation_records_counts_only_and_calls_nothing_else() -> None:
    """B9: no token needed; the outputs and tracked environments are `installation_gone`."""
    port = FakePort(
        outs=[_out("TOP-SECRET-NAME"), _out("V1", "variable")],
        envs=[_env("prod-private")],
        exists=False,
        untracked=2,
    )
    out = await GithubDeletionStep(port).run(_ctx())
    assert out.done
    assert out.skipped == [
        {"category": "github_outputs", "reason": "installation_gone", "count": 2},
        {"category": "github_environments", "reason": "installation_gone", "count": 1},
        {"category": "environment_kept", "reason": "untracked", "count": 2},
    ]
    assert port.calls == ["installation"]
    assert port.deleted == [] and port.released == []
    assert "TOP-SECRET-NAME" not in repr(out) and "prod-private" not in repr(out)


async def test_a_gone_installation_with_only_outputs_omits_the_environment_entry() -> None:
    """Categories with nothing recorded are not listed."""
    port = FakePort(outs=[_out("S1")], exists=False)
    out = await GithubDeletionStep(port).run(_ctx())
    assert out.skipped == [
        {"category": "github_outputs", "reason": "installation_gone", "count": 1}
    ]


async def test_a_failing_installation_lookup_is_transient() -> None:
    """GitHub down or the host not ready: the error reaches the engine, nothing is removed."""
    port = FakePort(outs=[_out("S1")], exists_error=RuntimeError("down"))
    with pytest.raises(RuntimeError, match="down"):
        await GithubDeletionStep(port).run(_ctx())
    assert len(port.outs) == 1


async def test_only_environments_named_by_rows_are_recorded_as_kept_untracked() -> None:
    """An environment without a tracking row is never deleted, only counted, on the last pass."""
    port = FakePort(untracked=3)
    out = await GithubDeletionStep(port).run(_ctx())
    assert out.done
    assert out.skipped == [{"category": "environment_kept", "reason": "untracked", "count": 3}]
    assert port.deleted == [] and port.calls == []


async def test_an_environment_created_within_the_skew_margin_is_still_ours() -> None:
    """GitHub's clock may run ahead of the row's by up to `CREATED_AT_SKEW`."""
    port = FakePort(envs=[_env("prod")], github_created={"prod": ROW_TIME + CREATED_AT_SKEW})
    out = await GithubDeletionStep(port).run(_ctx())
    assert port.deleted == ["prod"]
    assert out.counts == {"github_environments_removed": 1}
    assert out.skipped == []


async def test_a_recreated_environment_is_kept_recorded_and_forgotten() -> None:
    """GitHub's `created_at` later than row + margin: the user's own environment is kept."""
    later = ROW_TIME + CREATED_AT_SKEW + timedelta(seconds=1)
    port = FakePort(envs=[_env("prod"), _env("old")], github_created={"prod": later})
    out = await GithubDeletionStep(port).run(_ctx())
    assert port.deleted == ["old"]
    assert port.released == ["prod", "old"]
    assert out.counts == {"github_environments_removed": 1}
    assert out.skipped == [{"category": "environment_kept", "reason": "recreated", "count": 1}]
    assert out.cursor == {"handled": 2} and not out.done


async def test_an_environment_already_gone_counts_as_done() -> None:
    """A 404 (None) is done: nothing to delete, the row is forgotten."""
    port = FakePort(envs=[_env("prod")], github_created={"prod": None})
    out = await GithubDeletionStep(port).run(_ctx())
    assert port.deleted == [] and port.released == ["prod"]
    assert out.counts == {"github_environments_removed": 1}


async def test_a_crash_after_a_removal_converges() -> None:
    """The work list is re-read: a removed item is not listed again, the rest still goes."""
    port = FakePort(outs=[_out("S1"), _out("S2")], envs=[_env("prod")])
    port.outs.pop(0)  # a previous run removed S1, then died before saving the cursor
    outcomes = await _drain(port)
    assert port.calls.count("remove:S1") == 0
    assert outcomes[0].counts == {"github_outputs_removed": 1, "github_environments_removed": 1}
    assert outcomes[-1].done


async def test_a_failing_item_does_not_hold_the_others() -> None:
    """One failure: the rest are removed and the pass reports progress."""
    port = FakePort(outs=[_out("S1"), _out("S2")], envs=[_env("prod")], fail={"S1": OSError("x")})
    out = await GithubDeletionStep(port).run(_ctx())
    assert out.counts == {"github_outputs_removed": 1, "github_environments_removed": 1}
    assert [o.name for o in port.outs] == ["S1"]
    assert out.cursor == {"handled": 2}


async def test_a_pass_where_every_item_fails_holds_with_type_only_diagnostics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """All failed: the transient error for the engine; no name, message or value leaks."""
    port = FakePort(
        outs=[_out("S-hunter2")],
        envs=[_env("prod-hunter2")],
        fail={"S-hunter2": RuntimeError("tok=hunter2"), "prod-hunter2": ValueError("tok=hunter2")},
    )
    ctx = _ctx(cursor={"handled": 5})
    with caplog.at_level(logging.WARNING), pytest.raises(GithubStepHeldError) as held:
        await GithubDeletionStep(port).run(ctx)
    assert str(held.value) == "2 of 2 items failed (RuntimeError x1, ValueError x1)"
    assert "hunter2" not in caplog.text and "hunter2" not in str(held.value)
    assert "RuntimeError" in caplog.text
    assert ctx.record.step_cursor == {"handled": 5}  # nothing returned: attempts keep counting


async def test_the_start_rotates_by_attempts() -> None:
    """Items stuck at the head are not tried first after held attempts."""
    port = FakePort(outs=[_out("S1"), _out("S2"), _out("S3")])
    await GithubDeletionStep(port).run(_ctx(attempts=1))
    assert [c for c in port.calls if c.startswith("remove")] == [
        "remove:S2",
        "remove:S3",
        "remove:S1",
    ]


async def test_budget_exhaustion_mid_pass_reports_what_settled() -> None:
    """The budget ends the pass, with the progress made so far."""
    port = FakePort(outs=[_out("S1"), _out("S2")])
    out = await GithubDeletionStep(port).run(_ctx(budget=1))
    assert out.counts == {"github_outputs_removed": 1} and not out.done


async def test_budget_exhaustion_before_any_removal_propagates() -> None:
    """Nothing settled and nothing failed: release the lease."""
    port = FakePort(outs=[_out("S1")])
    with pytest.raises(RunBudgetExhausted):
        await GithubDeletionStep(port).run(_ctx(budget=0))


async def test_budget_spent_on_failures_holds() -> None:
    """A failure then the budget: held, so the attempt is counted and the start rotates."""
    port = FakePort(outs=[_out("S1"), _out("S2")], fail={"S1": RuntimeError("x")})
    with pytest.raises(GithubStepHeldError, match="1 of 2 items failed"):
        await GithubDeletionStep(port).run(_ctx(budget=1))


@pytest.mark.parametrize("error", [LeaseLost(), RunBudgetExhausted(), NonRetryableError("code")])
async def test_control_flow_errors_are_not_item_failures(error: Exception) -> None:
    """Lease, budget and non-retryable errors from the port propagate unchanged."""
    port = FakePort(outs=[_out("S1")], fail={"S1": error})
    with pytest.raises(type(error)):
        await GithubDeletionStep(port).run(_ctx())


async def test_a_pass_is_bounded_in_items() -> None:
    """At most `MAX_TRIES_PER_PASS` items are tried in one pass."""
    port = FakePort(outs=[_out(f"S{i}") for i in range(MAX_TRIES_PER_PASS + 5)])
    out = await GithubDeletionStep(port).run(_ctx())
    assert out.counts == {"github_outputs_removed": MAX_TRIES_PER_PASS}
    assert len(port.outs) == 5


async def test_the_cursor_accumulates_across_passes() -> None:
    """`handled` is the saved count plus this pass's."""
    port = FakePort(outs=[_out("S1")])
    out = await GithubDeletionStep(port).run(_ctx(cursor={"handled": 7}))
    assert out.cursor == {"handled": 8}
